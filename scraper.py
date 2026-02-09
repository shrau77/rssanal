import os
import json
import uuid
import time
import logging
import re
from io import BytesIO
from datetime import datetime

# Сторонние библиотеки
import feedparser
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from huggingface_hub import InferenceClient
from github import Github
from curl_cffi import requests as cf_requests

# --- CONFIG ---
# В GitHub Actions секреты прокидываются через env
HF_TOKEN = os.environ.get("HF_TOKEN")
GITHUB_TOKEN = os.environ.get("MY_GITHUB_TOKEN") # Используем PAT
REPO_NAME = os.environ.get("GITHUB_REPOSITORY")  # Автоматическая переменная "user/repo"
MODEL_ID = "Qwen/Qwen2.5-72B-Instruct" 
RSS_URL = "https://ntc.party/posts.rss"

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

KEYWORDS = [
    "vless", "vmess", "trojan", "fragment", "mtu", "noise", "packet-len", 
    "mtn", "beeline", "megafon", "mts", "tele2", "yota", "rostelecom", 
    "shadowsocks", "pbkdf2", "argon2", "hysteria", "amnezia", "xray", "sing-box",
    "reality", "grpc", "ws", "tcp", "warp", "wireguard"
]

class NetworkFetcher:
    """curl_cffi для обхода Cloudflare. На GitHub Actions работает стабильно."""
    @staticmethod
    def get(url):
        try:
            return cf_requests.get(
                url, 
                impersonate="chrome120", 
                timeout=30,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9"
                }
            )
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

class OCRProcessor:
    @staticmethod
    def extract_text_from_image_url(url):
        try:
            response = NetworkFetcher.get(url)
            if response and response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                # tesseract должен быть установлен в системе (в workflow yml)
                text = pytesseract.image_to_string(img, lang='rus+eng')
                return text
        except Exception as e:
            logger.error(f"OCR Error: {e}")
        return ""

class AIAnalyst:
    def __init__(self):
        self.client = InferenceClient(model=MODEL_ID, token=HF_TOKEN)

    def analyze(self, text):
        prompt = f"""
Ты парсер форума. Извлеки технические данные конфигов VPN.
Верни ТОЛЬКО валидный JSON.

Правила:
1. "type": "CONFIG" (протоколы), "COSMETICS" (настройки типа mtu), "FULL" (оба), "EXTERNAL" (ссылки), "GARBAGE".
2. "region"/"provider": Определи из текста (MTS, Beeline, Rostelecom, Moscow, SPb). Транслит (MTS_Moscow). Если нет - null.
3. "config": Полная строка конфига или JSON.
4. "cosmetics": Поля fragment, mtu, noise, split-http.
5. "summary": Краткое описание на русском.

Schema:
{{
  "type": "string",
  "region": "string or null",
  "provider": "string or null",
  "config": "string or null",
  "cosmetics": {{ "fragment": "string", "mtu": "int", "noise": "string" }},
  "summary": "string",
  "source_url": "string"
}}

Текст:
{text[:4000]} 
""" 
        try:
            response = self.client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1500,
                temperature=0.1
            )
            content = response.choices[0].message.content
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            return None
        except Exception as e:
            logger.error(f"AI Error: {e}")
            return None

class GitHubManager:
    def __init__(self):
        self.gh = Github(GITHUB_TOKEN)
        self.repo = self.gh.get_repo(REPO_NAME)

    def save_data(self, ai_data, meta_data):
        try:
            region = ai_data.get('region') or "Unknown"
            provider = ai_data.get('provider') or "Unknown"
            
            clean_reg = "".join([c for c in region if c.isalnum() or c in (' ', '-', '_')]).strip()
            clean_prov = "".join([c for c in provider if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            # Уникальное имя файла
            filename = f"{int(time.time())}_{str(uuid.uuid4())[:6]}.json"
            path = f"configs/{clean_reg}/{clean_prov}/{filename}"

            final_json = { "meta": meta_data, "data": ai_data }
            content = json.dumps(final_json, ensure_ascii=False, indent=2)
            
            self.repo.create_file(path=path, message=f"Add: {clean_prov}", content=content)
            logger.info(f"GitHub Saved: {path}")
            return True
        except Exception as e:
            logger.error(f"GitHub Save Error: {e}")
            return False

def main():
    logger.info("--- Starting GitHub Actions Scraper ---")
    
    # 1. Получаем RSS
    resp = NetworkFetcher.get(RSS_URL)
    if not resp or resp.status_code != 200:
        logger.error("Failed to fetch RSS")
        return

    feed = feedparser.parse(resp.content)
    logger.info(f"Entries found: {len(feed.entries)}")
    
    # Чтобы не обрабатывать старье, можно проверять дату, 
    # но проще полагаться на дедупликацию файла (если он уже есть, github вернет ошибку, которую мы ловим)
    # или просто обрабатывать последние 10 постов.
    for entry in feed.entries[:15]: 
        try:
            guid = entry.get('id', entry.get('link'))
            logger.info(f"Processing: {entry.title}")

            soup = BeautifulSoup(entry.description, 'lxml')
            text_content = soup.get_text(separator="\n")
            
            ocr_text = ""
            for img in soup.find_all('img'):
                src = img.get('src')
                if src:
                    if src.startswith('/'): src = "https://ntc.party" + src
                    if "emoji" not in src:
                        ocr_text += OCRProcessor.extract_text_from_image_url(src) + "\n"

            full_text = f"{entry.title}\n{text_content}\nOCR:\n{ocr_text}"

            if not any(k in full_text.lower() for k in KEYWORDS):
                continue

            analyst = AIAnalyst()
            result = analyst.analyze(full_text)
            
            if result and result.get('type') != 'GARBAGE':
                result['source_url'] = entry.link
                gh = GitHubManager()
                meta = { "guid": guid, "date": datetime.now().isoformat(), "host": "GitHub Actions" }
                gh.save_data(result, meta)
                
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Entry Error: {e}")

if __name__ == "__main__":
    main()
