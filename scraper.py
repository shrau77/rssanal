import os
import json
import uuid
import time
import logging
import re
from io import BytesIO
from datetime import datetime

import feedparser
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from huggingface_hub import InferenceClient
from github import Github
from playwright.sync_api import sync_playwright

# --- CONFIG ---
HF_TOKEN = os.environ.get("HF_TOKEN")
GITHUB_TOKEN = os.environ.get("MY_GITHUB_TOKEN")
REPO_NAME = os.environ.get("GITHUB_REPOSITORY")
MODEL_ID = "Qwen/Qwen2.5-72B-Instruct" 
RSS_URL = "https://ntc.party/posts.rss"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

KEYWORDS = [
    "vless", "vmess", "trojan", "fragment", "mtu", "noise", "packet-len", 
    "mtn", "beeline", "megafon", "mts", "tele2", "yota", "rostelecom", 
    "shadowsocks", "pbkdf2", "argon2", "hysteria", "amnezia", "xray", "sing-box",
    "reality", "grpc", "ws", "tcp", "warp", "wireguard"
]

class BrowserFetcher:
    """Использует Playwright (Chromium) для прохождения JS-челленджа Cloudflare"""
    
    @staticmethod
    def get_content(url, is_binary=False):
        logger.info(f"Launching Browser for: {url}")
        with sync_playwright() as p:
            # Запускаем браузер с аргументами, скрывающими автоматизацию
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ]
            )
            # Эмулируем реальный десктоп
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080}
            )
            page = context.new_page()
            
            try:
                # Переходим на сайт
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                
                # Ждем 5-10 секунд, пока Cloudflare крутит проверку "Just a moment..."
                logger.info("Waiting for Cloudflare challenge...")
                page.wait_for_timeout(8000) 
                
                # Если это картинка (бинарник), качаем через request context с куками
                if is_binary:
                    # Берем куки из страницы, которая прошла проверку
                    cookies = context.cookies()
                    # Формируем запрос
                    response = page.request.get(url)
                    if response.status == 200:
                        return response.body() # Возвращаем bytes
                    return None
                
                # Если это текст (RSS)
                content = page.content()
                
                # Иногда Playwright возвращает HTML обертку вокруг XML. 
                # Если мы видим, что content содержит RSS теги, но завернут в HTML, feedparser сам разберется.
                return content
                
            except Exception as e:
                logger.error(f"Browser Error: {e}")
                return None
            finally:
                browser.close()

class OCRProcessor:
    @staticmethod
    def extract_text_from_image_url(url):
        try:
            # Качаем картинку браузером, чтобы куки CF подцепились
            image_bytes = BrowserFetcher.get_content(url, is_binary=True)
            if image_bytes:
                img = Image.open(BytesIO(image_bytes))
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
    logger.info("--- Starting Scraper (Browser Mode) ---")
    
    # 1. RSS через Браузер
    html_content = BrowserFetcher.get_content(RSS_URL)
    
    if not html_content:
        logger.error("Failed to load RSS via Browser")
        return

    # feedparser умеет искать RSS внутри HTML, если Cloudflare отдал страницу с XML внутри
    feed = feedparser.parse(html_content)
    logger.info(f"Entries found: {len(feed.entries)}")
    
    if len(feed.entries) == 0:
        logger.warning("No entries found. Maybe Cloudflare is still blocking or structure changed.")
        # Логгируем первые 500 символов, чтобы понять, что вернулось (HTML капчи?)
        logger.info(f"Content preview: {str(html_content)[:500]}")
        return

    for entry in feed.entries[:10]: # Берем последние 10
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
                meta = { "guid": guid, "date": datetime.now().isoformat(), "host": "GH Actions + Playwright" }
                gh.save_data(result, meta)
                
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Entry Error: {e}")

if __name__ == "__main__":
    main()
