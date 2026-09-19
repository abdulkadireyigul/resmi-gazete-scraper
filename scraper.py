import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import re
import os
import sys
import json
import traceback
from urllib.parse import urljoin
from feedgen.feed import FeedGenerator

# --- Ayarlar ve Sabitler ---
RESMI_GAZETE_URL = "https://www.resmigazete.gov.tr"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
OUTPUT_RSS_FILE = "resmi_gazete.xml"
STATE_FILE = "last_processed.json"

# Cloudflare Worker relay URL'i (GitHub Actions'ta secrets.WORKER_URL olarak set edilir).
# Boşsa, doğrudan RESMI_GAZETE_URL'e istek atılır (yerel test için).
WORKER_URL = os.environ.get('WORKER_URL')

# Başlıktaki çeşitli tire/dash karakterlerini ve öncesindeki boşlukları (nbsp dahil) temizler.
# Kapsanan karakterler: hyphen-minus(-), en dash(–), em dash(—), horizontal bar(―),
# non-breaking hyphen(‑), figure dash(‒)
LEADING_DASH_RE = re.compile(r'^[\s\xa0]*[-\u2010\u2011\u2012\u2013\u2014\u2015]+[\s\xa0]*')

# Gazete numarasını başlıktan çıkarır. "ve <sayı> Sayılı" kalıbı, aradaki
# mükerrer notu (varsa) esnek şekilde tolere edilir.
GAZETTE_NUMBER_RE = re.compile(r've\s+(\d+)\s*(?:\([^)]*\))?\s*Sayılı')


def get_todays_entries():
    """
    Resmi Gazete ana sayfasını (gerekirse bir relay/proxy üzerinden) kontrol eder,
    başlıktan gazete numarasını çıkarır ve içindeki tüm maddelerin linklerini,
    başlıklarını ve gazete sayısını döndürür.
    """
    try:
        print("Fetching main page...")
        if WORKER_URL:
            print(f"Using relay: {WORKER_URL}")
            response = requests.get(WORKER_URL, timeout=60, headers=HEADERS)
        else:
            print("No WORKER_URL set, fetching directly (local test?).")
            response = requests.get(RESMI_GAZETE_URL, timeout=60, headers=HEADERS)
        response.raise_for_status()
        print("Main page fetched successfully.")

        soup = BeautifulSoup(response.content, 'lxml')

        title_span = soup.find('span', id='spanGazeteTarih')
        if not title_span:
            print("ERROR: Could not find the title span ('spanGazeteTarih'). "
                  "Site yapısı değişmiş ya da relay üzerinden beklenmeyen bir sayfa (captcha/engel) dönmüş olabilir.")
            return None, None

        title_text = title_span.text.strip()
        print(f"Found title: '{title_text}'")

        match = GAZETTE_NUMBER_RE.search(title_text)
        gazete_sayisi = match.group(1) if match else None
        if gazete_sayisi:
            print(f"Extracted gazette number: {gazete_sayisi}")
        else:
            print("Warning: Could not extract gazette number from title.")
            # Gazete sayısı olmadan state karşılaştırması yapılamaz, devam etmenin anlamı yok.
            return None, None

        content_div = soup.find('div', id='html-content')
        if not content_div:
            print("ERROR: Could not find the content div ('html-content').")
            return [], gazete_sayisi

        fihrist_items = content_div.find_all('div', class_='fihrist-item')
        if not fihrist_items:
            print("Warning: No content items ('fihrist-item') found.")
            return [], gazete_sayisi

        entries = []
        for item in fihrist_items:
            link_tag = item.find('a', href=True)
            if link_tag:
                raw_title = link_tag.text.strip()
                cleaned_title = LEADING_DASH_RE.sub('', raw_title)
                href = str(link_tag['href'])
                # urljoin, href'in mutlak/göreli olmasına bakılmaksızın doğru URL'i üretir.
                full_url = href if href.startswith('http') else urljoin(RESMI_GAZETE_URL + '/', href)

                entries.append({'title': cleaned_title, 'link': full_url})

        print(f"Successfully extracted {len(entries)} entries.")
        return entries, gazete_sayisi

    except requests.exceptions.Timeout:
        print("Error: Request timed out after 60 seconds.")
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching main page: {e}")
        return None, None
    except Exception:
        print("An unexpected error occurred during parsing:")
        traceback.print_exc()
        return None, None


def generate_rss_feed(entries, gazete_sayisi):
    """Verilen entry listesini kullanarak bir RSS feed oluşturur ve dosyaya yazar."""
    print(f"Generating RSS feed for gazette number {gazete_sayisi} with {len(entries)} items...")
    fg = FeedGenerator()
    fg.title('T.C. Resmî Gazete - Günlük İçerik')
    fg.link(href=RESMI_GAZETE_URL, rel='alternate')
    fg.description('Resmî Gazete\'de bugün yayınlanan duyurular.')
    fg.language('tr')

    feed_id = f"{RESMI_GAZETE_URL}/{datetime.now().strftime('%Y-%m-%d')}/{gazete_sayisi or 'unknown'}"
    fg.id(feed_id)
    fg.updated(datetime.now(timezone.utc))

    for entry in entries:
        fe = fg.add_entry()
        fe.id(entry['link'])
        fe.title(entry['title'])
        fe.link(href=entry['link'])
        fe.description(entry['title'])
        fe.pubDate(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))

    try:
        fg.rss_file(OUTPUT_RSS_FILE, pretty=True)
        print(f"RSS feed successfully generated and saved to {OUTPUT_RSS_FILE}")
    except Exception:
        print("Error writing RSS file:")
        traceback.print_exc()
        raise  # dosya yazılamadıysa state'i de güncellememek için hatayı yukarı fırlat


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
    except Exception:
        print(f"Error writing state file {STATE_FILE}:")
        traceback.print_exc()
        raise


# --- Ana Çalıştırma Bloğu ---
if __name__ == "__main__":
    print(f"\n--- Starting Scraper: {datetime.now()} ---")

    last_processed_number = load_last_processed_state()
    print(f"Last processed gazette number from state file: {last_processed_number}")

    todays_entries, current_gazette_number = get_todays_entries()

    exit_code = 0

    if todays_entries is None:
        print("\nFailed to fetch or process today's gazette. Exiting with error.")
        exit_code = 1
    elif current_gazette_number is None:
        print("\nCould not determine current gazette number. Cannot check if already processed. Exiting with error.")
        exit_code = 1
    elif current_gazette_number == last_processed_number:
        print(f"\nCurrent gazette number ({current_gazette_number}) is the same as the last processed one. "
              f"No new feed generation needed. Exiting normally.")
    elif not todays_entries:
        print("\nFound today's title but no entries. Feed will not be generated. State not updated.")
        print("This might happen if the content is published later. Will retry next run.")
    else:
        print(f"\nNew gazette number ({current_gazette_number}) detected (last was {last_processed_number}).")
        try:
            generate_rss_feed(todays_entries, current_gazette_number)
            save_last_processed_state(current_gazette_number)
        except Exception:
            print("\nFailed to generate feed or save state. Exiting with error.")
            exit_code = 1

    print(f"--- Scraper Finished: {datetime.now()} ---")
    sys.exit(exit_code)