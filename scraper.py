import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone # timezone eklendi
import locale
import re
import os # Dosya işlemleri için os eklendi
import json # Durum takibi için json eklendi
from feedgen.feed import FeedGenerator # feedgen eklendi

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Ayarlar ve Sabitler ---
# Türkçe tarihleri parse edebilmek için locale ayarı
try:
    locale.setlocale(locale.LC_TIME, 'tr_TR.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'tr_TR')
    except locale.Error:
        print("Warning: Turkish locale not found. Date parsing might fail.")

RESMI_GAZETE_URL = "https://www.resmigazete.gov.tr"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
OUTPUT_RSS_FILE = "resmi_gazete.xml" # Oluşturulacak RSS dosyasının adı
STATE_FILE = "last_processed.json" # Son işlenen gazete bilgisini tutacak dosya

# --- Ana Fonksiyonlar ---

def get_todays_entries():
    """
    Resmi Gazete ana sayfasını kontrol eder, bugünün yayınını doğrular
    ve içindeki tüm maddelerin linklerini, başlıklarını ve gazete sayısını döndürür.
    """
    
    # 1. GitHub Secrets'tan (ortam değişkenleri yoluyla) 4 bilgiyi oku
    proxy_host = os.environ.get('PROXY_HOST')
    proxy_port = os.environ.get('PROXY_PORT')
    proxy_user = os.environ.get('PROXY_USERNAME')
    proxy_pass = os.environ.get('PROXY_PASSWORD')

    proxies = None # Başlangıçta proxy yok
    
    # 4 değişkenin tamamı ortamda mevcutsa proxy'yi ayarla
    if proxy_host and proxy_port and proxy_user and proxy_pass:
        # Python 'requests' kütüphanesi için URL'i bu formatta birleştirmemiz gerekiyor:
        # http://kullaniciadi:sifre@host:port
        proxy_url = f"http://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
        
        # Requests modülünün anlayacağı proxy dict'ini oluştur
        proxies = {
            'http': proxy_url,
            'https': proxy_url # https siteleri için de aynı proxy'yi kullan
        }
        print(f"Proxy credentials found. Using Bright Data proxy: {proxy_host}:{proxy_port}")
    else:
        print("Proxy credentials not found in environment. Running without proxy (local test?).")
    
    try:
        # print("Fetching main page...")
        # response = requests.get(RESMI_GAZETE_URL, timeout=30, headers=HEADERS)
        
        print("Fetching main page through CORS proxy...")
        # proxy yerine CORS proxy kullanıyoruz
        proxied_url = f"https://api.cors.lol/?url={RESMI_GAZETE_URL}"
        
        response = requests.get(
            proxied_url, 
            timeout=60, 
            headers=HEADERS, 
            proxies=proxies,
            verify=False
        )
        response.raise_for_status()
        # print("Main page fetched successfully.")
        print("Main page fetched successfully via CORS proxy.")

        soup = BeautifulSoup(response.content, 'lxml')

        title_span = soup.find('span', id='spanGazeteTarih')
        if not title_span:
            print("ERROR: Could not find the title span ('spanGazeteTarih').")
            return None, None # Hem entries hem gazete_sayisi için None döndür

        title_text = title_span.text.strip()
        print(f"Found title: '{title_text}'")

        # Gazete sayısını başlık metninden çıkarmaya çalışalım (Regex ile)
        # Önceki Hatalı Desen: match = re.search(r'sayılı (\d+)', title_text)
        # Doğru Desen: Rakamları "ve " ile " Sayılı" arasında ara
        match = re.search(r've (\d+) Sayılı', title_text) # <-- BU SATIRI GÜNCELLE
        gazete_sayisi = match.group(1) if match else None
        if gazete_sayisi:
             print(f"Extracted gazette number: {gazete_sayisi}")
        else:
             print("Warning: Could not extract gazette number from title.")
             # Gazete sayısı olmadan devam etmenin anlamı yok, çünkü state'i karşılaştıramayız
             return None, None


        # try:
        #     today_str_pattern = datetime.now().strftime("%#d %B %Y").lower()
        # except ValueError:
        #      try:
        #         today_str_pattern = datetime.now().strftime("%-d %B %Y").lower()
        #      except ValueError:
        #         print("Warning: Failed to format date correctly. Using fallback.")
        #         today_str_pattern = datetime.now().strftime("%d %B %Y").lower()

        # if today_str_pattern not in title_text.lower():
        #     print(f"Today's date pattern '{today_str_pattern}' not found in the title.")
        #     return None, gazete_sayisi # Yayınlanmamış ama sayıyı bulduysak döndürelim

        # print("Today's date confirmed in the title.")

        content_div = soup.find('div', id='html-content')
        if not content_div:
            print("ERROR: Could not find the content div ('html-content').")
            return [], gazete_sayisi # Başlık var ama içerik yok, boş liste döndür

        fihrist_items = content_div.find_all('div', class_='fihrist-item')
        if not fihrist_items:
            print("Warning: No content items ('fihrist-item') found.")
            return [], gazete_sayisi

        entries = []
        for item in fihrist_items:
            link_tag = item.find('a', href=True)
            if link_tag:
                raw_title = link_tag.text.strip()
                cleaned_title = re.sub(r'^[–—-]\s*', '', raw_title)                
                href_value = link_tag['href']
                # Explicitly cast href_value to string to satisfy Pylance
                href = str(href_value) 
                full_url = href if href.startswith('http') else RESMI_GAZETE_URL + href

                entries.append({'title': cleaned_title, 'link': full_url})

        print(f"Successfully extracted {len(entries)} entries.")
        return entries, gazete_sayisi # Hem listeyi hem sayıyı döndür

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out after 60 seconds.")
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching main page: {e}")
        return None, None
    except Exception as e:
        print(f"An unexpected error occurred during parsing: {e}")
        return None, None

def generate_rss_feed(entries, gazete_sayisi):
    """Verilen entry listesini kullanarak bir RSS feed oluşturur ve dosyaya yazar."""
    print(f"Generating RSS feed for gazette number {gazete_sayisi} with {len(entries)} items...")
    fg = FeedGenerator()
    fg.title('T.C. Resmî Gazete - Günlük İçerik')
    fg.link(href=RESMI_GAZETE_URL, rel='alternate')
    fg.description('Resmî Gazete\'de bugün yayınlanan duyurular.')
    fg.language('tr')

    # Feed için benzersiz bir ID ve güncellenme zamanı ekleyelim
    feed_id = f"{RESMI_GAZETE_URL}/{datetime.now().strftime('%Y-%m-%d')}/{gazete_sayisi or 'unknown'}"
    fg.id(feed_id)
    fg.updated(datetime.now(timezone.utc)) # RSS için UTC zaman kullanmak standarttır

    for entry in entries:
        fe = fg.add_entry()
        # Her madde için benzersiz bir ID oluşturalım (link yeterli)
        fe.id(entry['link'])
        fe.title(entry['title'])
        fe.link(href=entry['link'])
        # Açıklama olarak başlığı tekrar kullanabiliriz veya boş bırakabiliriz
        fe.description(entry['title'])
        # Yayınlanma tarihi olarak bugünü ekleyebiliriz (saat bilgisi olmadan)
        fe.pubDate(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))

    # RSS feed'ini dosyaya yaz
    try:
        fg.rss_file(OUTPUT_RSS_FILE, pretty=True) # pretty=True okunabilir XML üretir
        print(f"RSS feed successfully generated and saved to {OUTPUT_RSS_FILE}")
    except Exception as e:
        print(f"Error writing RSS file: {e}")

def load_last_processed_state():
    """Son işlenen gazete sayısını state dosyasından okur."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                return state.get('last_processed_gazette_number')
        except Exception as e:
            print(f"Error reading state file {STATE_FILE}: {e}")
    return None

def save_last_processed_state(gazete_sayisi):
    """İşlenen gazete sayısını state dosyasına yazar."""
    try:
        state = {'last_processed_gazette_number': gazete_sayisi}
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        print(f"Successfully saved state: processed gazette number {gazete_sayisi}")
    except Exception as e:
        print(f"Error writing state file {STATE_FILE}: {e}")

# --- Ana Çalıştırma Bloğu ---
if __name__ == "__main__":
    print(f"\n--- Starting Scraper: {datetime.now()} ---")

    # 1. Son işlenen gazete sayısını oku
    last_processed_number = load_last_processed_state()
    print(f"Last processed gazette number from state file: {last_processed_number}")

    # 2. Bugünün entry'lerini ve gazete sayısını çek
    todays_entries, current_gazette_number = get_todays_entries()

    # 3. İşlem yapılıp yapılmayacağına karar ver
    if todays_entries is None:
        print("\nFailed to fetch or process today's gazette. Exiting.")
    elif current_gazette_number is None:
        print("\nCould not determine current gazette number. Cannot check if already processed. Exiting.")
    elif current_gazette_number == last_processed_number:
        print(f"\nCurrent gazette number ({current_gazette_number}) is the same as the last processed one. No new feed generation needed. Exiting.")
    elif not todays_entries: # Boş liste geldi (başlık bulundu ama içerik yoktu)
         print("\nFound today's title but no entries. Feed will not be generated. State not updated.")
         print("This might happen if the content is published later. Will retry next run.")
    else:
        # Yeni gazete bulundu ve içinde entry'ler var!
        print(f"\nNew gazette number ({current_gazette_number}) detected (last was {last_processed_number}).")
        
        # 4. RSS Feed'ini oluştur/güncelle
        generate_rss_feed(todays_entries, current_gazette_number)
        
        # 5. Yeni durumu kaydet
        save_last_processed_state(current_gazette_number)

    print(f"--- Scraper Finished: {datetime.now()} ---")