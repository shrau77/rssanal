import os
import json
import uuid
import time
import logging
import re
import random
from io import BytesIO
from datetime import datetime

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from huggingface_hub import InferenceClient
from github import Github
from curl_cffi import requests as cf_requests

# --- CONFIG ---
HF_TOKEN = os.environ.get("HF_TOKEN")
GITHUB_TOKEN = os.environ.get("MY_GITHUB_TOKEN")
REPO_NAME = os.environ.get("GITHUB_REPOSITORY")
MODEL_ID = "Qwen/Qwen2.5-72B-Instruct" 
RSS_URL = "https://ntc.party/posts.rss"

# Списки живых прокси (HTTP/HTTPS)
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

KEYWORDS = [
    "vless", "vmess", "trojan", "fragment", "mtu", "noise", "packet-len", 
    "mtn", "beeline", "megafon", "mts", "tele2", "yota", "rostelecom", 
    "shadowsocks", "pbkdf2", "argon2", "hysteria", "amnezia", "xray", "sing-box",
    "reality", "grpc", "ws", "tcp", "warp", "wireguard"
]

class ProxyManager:
    """Менеджер для поиска живого прокси"""
    def __init__(self):
        self.proxies = []

    def fetch_proxies(self):
        logger.info("Fetching fresh proxies...")
        for source in PROXY_SOURCES:
            try:
                r = requests.get(source, timeout=10)
                if r.status_code == 200:
                    lines = r.text.strip().split('\n')
                    logger.info(f"Loaded {len(lines)} proxies from {source}")
                    self.proxies.extend(lines)
            except Exception as e:
                logger.error(f"Error fetching proxy list: {e}")
        
        # Перемешиваем, чтобы не брать одни и те же
        random.shuffle(self.proxies)
        self.proxies = list(set(self.proxies)) # Удаляем дубли
        logger.info(f"Total unique proxies to try: {len(self.proxies)}")

    def get_working_session(self, test_url):
        """Перебирает прокси, пока не найдет рабочий для curl_cffi"""
        # Сначала пробуем без прокси (вдруг повезет?)
        try:
            logger.info("Trying direct connection...")
            sess = cf_requests.Session(impersonate="chrome120")
            resp = sess.get(test_url, timeout=10)
            if resp.status_code == 200:
                logger.info("Direct connection worked!")
                return sess
        except Exception:
            logger.info("Direct connection failed. Starting Proxy Roulette...")

        # Пробуем прокси
        # Ограничим попытки, чтобы не висеть вечно (например, 20 попыток)
        max_tries = 30
        for i, proxy_addr in enumerate(self.proxies[:max_tries]):
            proxy_url = f"http://{proxy_addr.strip()}"
            logger.info(f"[{i+1}/{max_tries}] Testing proxy: {proxy_url}")
            
            try:
                sess = cf_requests.Session(impersonate="chrome120")
                sess.proxies = {"http": proxy_url, "https": proxy_url}
                
                # Тестовый запрос
                resp = sess.get(test_url, timeout=15)
                
                if resp.status_code == 200:
                    logger.info(f"🎉 SUCCESS! Found working proxy: {proxy_url}")
                    return sess
                else:
                    logger.warning(f"Proxy returned status {resp.status_code}")
            
            except Exception as e:
                # Ошибки соединения игнорируем, просто идем к следующему
                pass
        
        raise Exception("All proxies failed. Cloudflare won today.")

# Глобальная сессия
proxy_manager = ProxyManager()
proxy_manager.fetch_proxies()
# Инициализируем сессию один раз
global_session = proxy_manager.get_working_session(RSS_URL)

class OCRProcessor:
    @staticmethod
    def extract_text_from_image_url(url):
        try:
            # Используем ту же сессию (тот же прокси) для картинок
            response = global_session.get(url, timeout=20)
            if response and response.status_code == 200:
                img = Image.open(BytesIO(response.content))
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
2. "region"/"provider": Определи из текста (MTS, Beeline, Rostelecom, Moscow, SPb). Транслит. Если нет - null.
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
    logger.info("--- Starting Scraper (Proxy Mode) ---")
    
    # 1. RSS через найденный прокси
    try:
        resp = global_session.get(RSS_URL, timeout=30)
        feed = feedparser.parse(resp.content)
        logger.info(f"Entries found: {len(feed.entries)}")
    except Exception as e:
        logger.error(f"Fatal: Could not fetch RSS even with proxies. {e}")
        return

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
                meta = { "guid": guid, "date": datetime.now().isoformat(), "host": "GH Actions + Proxy" }
                gh.save_data(result, meta)
                
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Entry Error: {e}")

if __name__ == "__main__":
    main()
