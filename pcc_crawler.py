#!/usr/bin/env python3
"""
起行採購監控爬蟲（雲林、彰化、嘉義）
執行方式：
  1. 自動：scheduled_update.py 由 launchd 觸發
  2. 手動：python3 pcc_crawler.py
"""

import re
import asyncio
import difflib
import hashlib
import json
import datetime
import os
import time
import fcntl
from playwright.async_api import async_playwright

try:
    from zoneinfo import ZoneInfo
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:
    # Windows 可能沒裝 tzdata；台灣無夏令時間，固定 +08:00 等價
    TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "data.json")
STATUS_FILE = os.path.join(os.path.dirname(__file__), ".status.json")
MAX_CONSECUTIVE_FAILURES = 3
MAX_FAILURE_RATIO = 0.5
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".crawler.lock")
# 關鍵機關查詢失敗時，資料照常寫入與發布，但 exit code 回 2，讓排程重跑
CRITICAL_ORGS = ["雲林縣政府"]
EXIT_CRITICAL_FAILED = 2
# 查過但查無歷史的活動案，這麼多天內不重查
HISTORY_RECHECK_DAYS = 7

PCC_BLOCK_MARKERS = (
    "web page blocked",
    "the url you requested has been blocked",
    "attack id",
)


class PccBlockedError(RuntimeError):
    """政府採購網回傳防護攔截頁，必須立即停止，不能再逐機關重試。"""


async def detect_pcc_block(page):
    """只回報攔截類型，不把 IP／Attack ID 等頁面細節寫進 log。"""
    try:
        title = await page.title()
        body = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None
    content = f"{title}\n{body}".lower()
    if any(marker in content for marker in PCC_BLOCK_MARKERS):
        return "政府採購網回傳 Web Page Blocked 防護頁"
    return None


def validate_crawl_results(total_orgs, failed_count, tender_count, existing_count):
    """拒絕用明顯異常的結果覆蓋上一份正常資料。"""
    if total_orgs <= 0:
        raise RuntimeError("監控機關清單為空，拒絕寫入資料")
    if failed_count >= total_orgs:
        raise RuntimeError(f"{total_orgs} 個機關全部查詢失敗，拒絕覆蓋舊資料")
    if failed_count / total_orgs >= MAX_FAILURE_RATIO:
        raise RuntimeError(
            f"{failed_count}/{total_orgs} 個機關查詢失敗，超過安全門檻，拒絕覆蓋舊資料"
        )
    if tender_count == 0 and existing_count > 0:
        raise RuntimeError(
            f"本次抓到 0 筆，但現有資料有 {existing_count} 筆，拒絕用空資料覆蓋"
        )


def report_progress(pct, msg):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({"progress": pct, "message": msg}, f)
    except Exception:
        pass
PCC_BASE = "https://web.pcc.gov.tw"
PCC_URL = f"{PCC_BASE}/prkms/tender/common/basic/indexTenderBasic"
PCC_FULLTEXT_URL = f"{PCC_BASE}/prkms/tender/common/bulletion/indexBulletion"
PCC_DETAIL_BASE = f"{PCC_BASE}/tps/atm/AtmAwardWithoutSso/QueryAtmAwardDetail"

# 2026-11-17 活動判定改成「強訊號／弱訊號／服務類否決字」三層（見 100_Todo/projects/2026-11-17_採購活動判定_新規則提案.md）：
# - 強訊號：標題有就是活動案，不會被排除字或否決字推翻（保住「耶誕嘉年華暨水岸光環境建置計畫」這類混合案）
# - 弱訊號：只靠它成立的案子，若同時命中否決字或工程排除字就不算活動（擋掉「志工團體意外保險」「靈堂佈置委託經營」）
# - 漏掉活動案比多收幾筆嚴重：弱訊號沒有否決字時仍算活動
# - 比對前先移除所有空白（「培 力」「(1 年期 )」這類排版空白不能讓判定失效）
STRONG_ACTIVITY_KEYWORDS = [
    "表演", "演出", "演唱", "舞蹈", "戲劇", "展覽", "特展", "策展",
    "活動", "節慶", "慶典", "祭典", "嘉年華", "晚會", "博覽會", "園遊會",
    "運動會", "競賽", "競技", "馬拉松", "論壇", "頒獎",
    "燈會", "燈節", "藝術節", "音樂節", "文化節", "電影節", "觀光節", "產業節",
    "市集", "踩街", "遶境", "進香", "走讀", "開幕", "典禮", "發表會", "成果展", "園遊",
    "工作坊", "講座", "研習", "展售", "導覽", "啟用", "新春", "元宵", "中秋", "跨年", "耶誕", "聖誕",
    "藝文", "古蹟日", "社區營造", "社造", "地方創生", "文史", "口述", "人文保存",
    "地方服務團", "社區規劃師",
]
WEAK_ACTIVITY_KEYWORDS = [
    "裝置", "行銷", "宣傳", "推廣", "宣導", "文化", "藝術", "音樂", "交流",
    "社區", "培力", "眷村", "部落", "燈籠", "彩繪",
    "農村再生", "社區規劃", "關懷據點", "樂齡", "長者", "共餐", "志工",
    "食農", "環境教育", "客庄", "客家", "原住民",
    "委外經營", "委託經營", "園區經營", "老宅",
    # 平面設計／印刷類（DAVID 本業）：單獨出現算弱訊號
    "設計", "印刷", "視覺", "海報", "文宣", "美編", "年曆", "門聯",
]
# 一般行政服務類字眼：只對「只靠弱訊號成立」的案子生效，不能否決強訊號
SERVICE_VETO_KEYWORDS = [
    "保險", "清潔", "保全", "審查", "重劃", "殯儀", "靈堂", "假牙", "清運", "清除", "垃圾",
    "資訊系統", "健檢", "疫苗", "採血", "保養", "消毒", "檢驗", "供餐", "午餐",
    "租賃", "影印", "車輛", "印表機", "文具",
]
# 舊名保留（其他程式或舊測試可能 import）
ACTIVITY_KEYWORDS = STRONG_ACTIVITY_KEYWORDS + WEAK_ACTIVITY_KEYWORDS

# 標題裡的機關名／路名本身含「文化」等字，比對前先剔除，避免誤判成活動案
ORG_NOISE = ["文化觀光處", "文化處", "文化局", "文化路"]

# 場館名稱，不是活動訊號。「社區活動中心」同時命中「社區」與「活動」兩個活動
# 關鍵字，會讓活動優先判定否決掉工程排除，使得社區活動中心的新建／修繕／監造案
# 全部被當成活動案放行（2026-09-02 實查到 2 筆）。只在活動判定前剔除，不能放進
# ORG_NOISE——「活動中心」本身是 EXCLUDE_KEYWORDS，剔掉會連排除規則一起失效。
# 長字串排前面，避免先被短的吃掉。
VENUE_NOISE = ["社區活動中心", "活動中心"]

# 標題含這些詞的多半是工程、修繕、設備或設施案，不是活動標案
EXCLUDE_KEYWORDS = [
    "工程", "監造", "修繕", "汰換", "整修", "新建", "改建", "增建", "興建",
    "開闢", "道路", "橋梁", "水利", "排水", "下水道", "污水", "雨水",
    "管線", "路面", "鋪面", "邊坡", "護岸", "堤防", "停車場",
    "建築物", "公寓大廈", "校舍", "廳舍", "辦公廳舍", "活動中心",
    "無障礙", "昇降設備", "電梯", "空調", "消防", "照明", "機電",
    "水電", "屋頂", "防水", "耐震", "結構補強",
    # 2026-08-21 補：實查 data.json 發現這些工程／設施案因字面差一字而漏擋
    "大排", "管道", "鋪設", "地坪", "拆除", "污泥", "土方", "清淤", "疏濬",
    "公共設施", "結構設施", "掩埋場", "水資源回收", "抽水站", "放流水",
    "環境監測", "環境清潔", "樹木修剪", "墓基", "公墓", "機房", "專爐",
    "環境影響評估", "基本設計", "廢家具",
]

YUNLIN_TOWNS = [
    "斗六市", "斗南鎮", "虎尾鎮", "西螺鎮", "土庫鎮", "北港鎮",
    "古坑鄉", "大埤鄉", "莿桐鄉", "林內鄉", "二崙鄉", "崙背鄉",
    "麥寮鄉", "東勢鄉", "褒忠鄉", "臺西鄉", "元長鄉", "四湖鄉",
    "口湖鄉", "水林鄉"
]

CHANGHUA_TOWNS = [
    "彰化市", "員林市", "和美鎮", "鹿港鎮", "溪湖鎮", "二林鎮",
    "田中鎮", "北斗鎮", "花壇鄉", "芬園鄉", "大村鄉", "永靖鄉",
    "伸港鄉", "線西鄉", "福興鄉", "秀水鄉", "埔心鄉", "埔鹽鄉",
    "大城鄉", "芳苑鄉", "竹塘鄉", "社頭鄉", "二水鄉", "田尾鄉",
    "埤頭鄉", "溪州鄉"
]

CHIAYI_TOWNS = [
    "太保市", "朴子市", "布袋鎮", "大林鎮", "民雄鄉", "溪口鄉",
    "新港鄉", "六腳鄉", "東石鄉", "義竹鄉", "鹿草鄉", "水上鄉",
    "中埔鄉", "竹崎鄉", "梅山鄉", "番路鄉", "大埔鄉", "阿里山鄉"
]

# 各區域的監控機關；三區都連鄉鎮市公所一起監控。
# 2026-08-31 依 DAVID 指示縮回雲林、彰化、嘉義三區（原含台中、台南兩個直轄市）
REGIONS = {
    "雲林": {
        "orgs": ["雲林縣政府"] + [t + "公所" for t in YUNLIN_TOWNS],
        "towns": YUNLIN_TOWNS,
        "fallback": "雲林縣",
    },
    "彰化": {
        "orgs": ["彰化縣政府"] + [t + "公所" for t in CHANGHUA_TOWNS],
        "towns": CHANGHUA_TOWNS,
        "fallback": "彰化縣",
    },
    "嘉義": {
        "orgs": ["嘉義縣政府", "嘉義市政府"] + [t + "公所" for t in CHIAYI_TOWNS],
        "towns": CHIAYI_TOWNS + ["嘉義市"],
        "fallback": "嘉義縣",
    },
}

_PLACE_WORDS = sorted(
    set(
        ["雲林縣政府", "雲林縣",
         "彰化縣政府", "彰化縣", "嘉義縣政府", "嘉義市政府", "嘉義縣", "嘉義市"]
        + YUNLIN_TOWNS + CHANGHUA_TOWNS + CHIAYI_TOWNS
    ),
    key=len, reverse=True,
)
TOWNS_PATTERN = r'^(' + '|'.join(_PLACE_WORDS) + r')'


def _strip_noise(title):
    """比對前的正規化：剔除機關名雜訊，並拿掉括號符號。

    括號會把字串打斷，像「廢（污）水」就比不到「污水」，所以只移除括號
    符號本身、保留裡面的字。
    """
    t = title
    for w in ORG_NOISE:
        t = t.replace(w, "")
    return re.sub(r'[（）()〔〕\[\]]', '', t)


def _compact(title):
    """比對用字串：去更正公告前綴、機關名雜訊、括號符號、場館名，並移除所有空白。"""
    t = _strip_noise(strip_correction_prefix(title))
    for w in VENUE_NOISE:
        t = t.replace(w, "")
    return re.sub(r"\s+", "", t)


def is_activity(title):
    """活動判定（2026-11-17 改為強弱訊號）：
    強訊號（或第N屆）→ 活動；弱訊號 → 沒有否決字也沒有工程排除字才算活動；都沒有 → 非活動。
    活動判定優先於工程排除的順序不變（見 is_excluded_tender）。
    """
    t = _compact(title)
    if any(kw in t for kw in STRONG_ACTIVITY_KEYWORDS) or re.search(ORDINAL_RE, t):
        return True
    if not any(kw in t for kw in WEAK_ACTIVITY_KEYWORDS):
        return False
    if any(kw in t for kw in SERVICE_VETO_KEYWORDS):
        return False
    return not any(kw in t for kw in EXCLUDE_KEYWORDS)


def is_excluded_tender(title):
    """工程／設施／設備案排除。只對「完全沒有活動特徵」的案子生效。"""
    if is_activity(title):
        return False
    t = re.sub(r"\s+", "", _strip_noise(strip_correction_prefix(title)))
    return any(kw in t for kw in EXCLUDE_KEYWORDS)


def town_from_org(org, region):
    cfg = REGIONS[region]
    for t in cfg["towns"]:
        if t in org:
            return t
    return cfg["fallback"]


# 「第二十二屆」這類屆次詞每年都變，且會讓縮短後的關鍵字撈到不相干的案子
ORDINAL_RE = r'第[0-9一二三四五六七八九十百]+屆'


CORRECTION_PREFIX_RE = r'^[（(]更正公告[）)]\s*'


def strip_correction_prefix(title):
    """去掉標題開頭的「(更正公告)」，避免被當成關鍵字或比對雜訊。"""
    return re.sub(CORRECTION_PREFIX_RE, '', title.strip())


def extract_keyword(title):
    """從標案名稱提取搜尋關鍵字"""
    title = strip_correction_prefix(title)
    kw = re.sub(r'\d{3,4}年度?|20\d{2}', '', title).strip()
    kw = re.sub(ORDINAL_RE, '', kw).strip()
    kw = re.sub(r'委託.*|勞務採購.*', '', kw).strip()
    kw = re.sub(r'執行案|計畫案|案$', '', kw).strip()
    kw = kw.replace('「', '').replace('」', '').strip()
    kw = re.sub(TOWNS_PATTERN, '', kw).strip()
    # 「暨」後面是副標題，截掉
    kw = re.sub(r'暨.*', '', kw).strip()
    # 取前6字，避免 keyword 太長比對失敗
    return kw[:6].strip()


def normalize_for_match(title):
    """去除標案編號、年份、屆次、委託尾綴等雜訊以利比對"""
    title = strip_correction_prefix(title)
    t = re.sub(r'^[0-9A-Za-z\-]+\s+', '', title).strip()  # 決標列表的標案編號前綴
    t = strip_correction_prefix(t)
    t = re.sub(r'\d{3,4}年度?|20\d{2}', '', t).strip()
    t = re.sub(ORDINAL_RE, '', t).strip()
    t = re.sub(r'委託.*|勞務採購.*', '', t).strip()
    t = t.replace('「', '').replace('」', '').strip()
    return t


def titles_similar(a, b, threshold=0.55):
    """標題相似度比對，容忍年度、機關前綴等差異"""
    na, nb = normalize_for_match(a), normalize_for_match(b)
    # 正規化後互為子字串（如「糖都嘉年華」⊂「台灣觀光100亮點-糖都嘉年華」）視為同案
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def clean_winner(name):
    """清理廠商名稱，去掉英文括號部分"""
    name = re.sub(r'\s*[\(\（].*', '', name).strip()
    return name[:20]


async def get_pk_from_href(href):
    """從 href 提取 pk 參數"""
    match = re.search(r'pk=([^&]+)', href)
    return match.group(1) if match else ""


async def get_winner_from_detail(context, pk):
    """從決標詳細頁抓得標廠商、決標金額、投標廠商家數、底價"""
    detail_url = f"{PCC_DETAIL_BASE}?pkAtmMain={pk}"
    winner = ""
    award_price = ""
    bidder_count = ""
    base_price = ""
    detail_page = None
    try:
        detail_page = await context.new_page()
        await detail_page.goto(detail_url, timeout=20000)
        await detail_page.wait_for_timeout(3000)

        content = await detail_page.inner_text("body")
        lines = [l.strip() for l in content.split('\n') if l.strip()]

        # 抓投標廠商家數與底價（頁面前段摘要區）
        for i, line in enumerate(lines):
            if line == '投標廠商家數' and i + 1 < len(lines):
                bidder_count = lines[i + 1].strip()[:10]
            if line == '底價' and i + 1 < len(lines):
                raw = lines[i + 1].strip()
                base_price = re.sub(r'[^\d,元].*', '', raw).strip()[:20]

        i = 0
        while i < len(lines):
            if lines[i] == '是否得標' and i + 1 < len(lines) and lines[i+1] == '是':
                for j in range(i-1, max(i-5, 0), -1):
                    if lines[j] == '廠商名稱' and j + 1 < len(lines):
                        winner = clean_winner(lines[j+1])
                        break
                for j in range(i, min(i+15, len(lines))):
                    if lines[j] == '決標金額' and j + 1 < len(lines):
                        raw = lines[j+1].split('\n')[0].strip()
                        award_price = re.sub(r'[^\d,元].*', '', raw).strip()[:20]
                        break
                if winner:
                    break
            i += 1

        if not winner:
            for i, line in enumerate(lines):
                if line == '得標廠商' and i + 1 < len(lines):
                    winner = clean_winner(lines[i+1])
                    break
    except Exception:
        pass
    finally:
        if detail_page is not None:
            try:
                await detail_page.close()
            except Exception:
                pass

    return winner, award_price, bidder_count, base_price


async def search_history_year(page, context, title, keyword, year):
    """用指定關鍵字在指定年度的決標公告中找一筆歷史記錄，找不到回傳 None"""
    await page.goto(PCC_FULLTEXT_URL, timeout=30000)
    await page.wait_for_timeout(4000)

    await page.fill('input[id="dep"]', keyword)

    checkboxes = await page.query_selector_all('input[name="tenderStatusType"]')
    for cb in checkboxes:
        val = await cb.get_attribute("value")
        await cb.evaluate(f"el => el.checked = {'true' if val == '決標' else 'false'}")

    await page.click(f'label[for="level_{year}"]')
    await page.wait_for_timeout(300)
    await page.evaluate("bulletionSearch()")
    await page.wait_for_timeout(5000)
    await page.wait_for_load_state("networkidle", timeout=20000)

    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) != 10:
            continue
        texts = [(await c.inner_text()).strip().replace('\n', ' ') for c in cells]
        row_title = texts[3] if len(texts) > 3 else ""
        award_date = texts[5] if len(texts) > 5 else ""

        # 關鍵字須命中，且整體標題也要夠相似——兩者都過才算同一案，
        # 避免「第二十二屆」這類碎詞撈到畢業紀念冊、金展獎等不相干決標
        if keyword not in normalize_for_match(row_title) or not titles_similar(title, row_title):
            continue
        if "無法決標" in award_date:
            continue

        view_link = await cells[9].query_selector("a")
        href = await view_link.get_attribute("href") if view_link else ""
        pk = await get_pk_from_href(href)

        if pk:
            winner, award_price, bidder_count, base_price = await get_winner_from_detail(context, pk)
            return {
                "year": year + 1911,
                "title": row_title,
                "winner": winner or "查無資料",
                "award_price": award_price or "查無資料",
                "bidder_count": bidder_count or "查無資料",
                "base_price": base_price or "未公開",
                "budget": "",
            }
        return None
    return None


def taipei_today():
    return datetime.datetime.now(TAIPEI).date()


def history_years(today=None):
    """歷史查詢年度（民國年）：今年、去年、前年。以台北時區為準。"""
    today = today or taipei_today()
    roc_year = today.year - 1911
    return [roc_year, roc_year - 1, roc_year - 2]


async def query_history(page, context, title, years=None):
    """用全文檢索查詢今年、去年、前年歷史得標。先用 6 字關鍵字查，查無時退用 4 字再查一次。"""
    if years is None:
        years = history_years()

    history = []
    keyword = extract_keyword(title)
    if not keyword:
        return history

    # 候選關鍵字：完整 6 字 → 前 4 字（去重）
    candidates = [keyword]
    if len(keyword) > 4 and keyword[:4] not in candidates:
        candidates.append(keyword[:4])

    for year in years:
        for kw in candidates:
            try:
                entry = await search_history_year(page, context, title, kw, year)
            except Exception as e:
                print(f"    {year}年查詢失敗：{e}")
                entry = None
            if entry:
                history.append(entry)
                break
            await asyncio.sleep(2)
        await asyncio.sleep(2)

    return history




# 採購網會擋過舊的 Chrome UA（2026-09-02 實測寫死的 Chrome/120 必被防護頁攔下），
# 也會擋 headless 預設的 HeadlessChrome。改成沿用實際安裝的 Chromium 版號，
# 這樣升級 Playwright 就會自動跟上，不必再手動改版本。
FALLBACK_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
MIN_CHROME_MAJOR = 140


async def resolve_user_agent(browser):
    """取實際 Chromium 的 UA，去掉 Headless 字樣；版本太舊或取不到就用備援 UA。"""
    try:
        probe_ctx = await browser.new_context()
        probe_page = await probe_ctx.new_page()
        ua = await probe_page.evaluate("navigator.userAgent")
        await probe_ctx.close()
    except Exception:
        return FALLBACK_UA
    ua = ua.replace("HeadlessChrome", "Chrome")
    match = re.search(r"Chrome/(\d+)", ua)
    if not match or int(match.group(1)) < MIN_CHROME_MAJOR:
        return FALLBACK_UA
    return ua


async def make_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=['--disable-blink-features=AutomationControlled']
    )
    context = await browser.new_context(
        user_agent=await resolve_user_agent(browser),
        viewport={'width': 1280, 'height': 800},
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


async def query_one_org(page, org_name, region):
    results = []
    try:
        await page.goto(PCC_URL, timeout=30000)
        await page.wait_for_selector('input[name="orgName"]', timeout=15000)

        await page.fill('input[name="orgName"]', org_name)
        await page.click('label[for="level_22"]')
        await page.wait_for_timeout(300)
        await page.click('label[for="RadProctrgCate3"]')
        await page.wait_for_timeout(300)
        await page.evaluate("basicTenderSearch()")
        await page.wait_for_timeout(5000)
        await page.wait_for_load_state("networkidle", timeout=20000)

        rows = await page.query_selector_all("tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) != 10:
                continue
            try:
                name_cell_text = (await cells[2].inner_text()).strip().replace('\n', ' ')
                parts = name_cell_text.split(' ', 1)
                tender_id = parts[0].strip() if parts else ""
                title = parts[1].strip() if len(parts) > 1 else name_cell_text

                if not title:
                    continue

                org = (await cells[1].inner_text()).strip()
                recruit_type = (await cells[4].inner_text()).strip().replace('\n', ' ')
                pub_date = (await cells[6].inner_text()).strip()
                deadline = (await cells[7].inner_text()).strip()
                budget = (await cells[8].inner_text()).strip()

                link = await cells[9].query_selector("a")
                href = await link.get_attribute("href") if link else ""
                url = f"{PCC_BASE}{href}" if href.startswith("/") else href

                results.append({
                    "id": tender_id or hashlib.md5((title + org_name).encode("utf-8")).hexdigest()[:12],
                    "title": title,
                    "org": org or org_name,
                    "region": region,
                    "town": town_from_org(org_name, region),
                    "type": "勞務",
                    "recruit_type": recruit_type,
                    "budget": budget,
                    "date": pub_date,
                    "deadline": deadline,
                    "status": "招標中",
                    "winner": "",
                    "url": url,
                    "history": [],
                    "is_activity": is_activity(title),
                    "src_org": org_name,
                })
            except Exception:
                continue

    except Exception as e:
        block_reason = await detect_pcc_block(page)
        if block_reason:
            raise PccBlockedError(block_reason) from e
        print(f"✗ {e}", end=" ")
        return None  # 查詢失敗（與「查到 0 筆」區分），讓呼叫端重試

    return results


async def query_org_with_retry(page, org_name, region, retries=1):
    """查詢失敗時自動重試，全部失敗回傳 None"""
    for attempt in range(retries + 1):
        results = await query_one_org(page, org_name, region)
        if results is not None:
            return results
        if attempt < retries:
            print("重試...", end=" ", flush=True)
            await asyncio.sleep(5)
    return None


def tender_key(t):
    """去重鍵：機關名＋案號（案號是各機關自編，不同機關可能撞號）。"""
    return (t.get("org", ""), t.get("id", ""))


def add_tender(bucket, t):
    """加入 bucket（dict，鍵為 tender_key）；已有同機關同案號就略過。回傳是否新增。"""
    key = tender_key(t)
    if key in bucket:
        return False
    bucket[key] = t
    return True


def ensure_unique_ids(tenders):
    """輸出的 id 要在整份資料內唯一：撞號時，後出現的在 id 後面加機關名短雜湊。"""
    seen = set()
    for t in tenders:
        base = t["id"]
        if base in seen:
            suffix = hashlib.md5(t.get("org", "").encode("utf-8")).hexdigest()[:4]
            new_id = f"{base}-{suffix}"
            n = 2
            while new_id in seen:
                new_id = f"{base}-{suffix}{n}"
                n += 1
            t["id"] = new_id
        seen.add(t["id"])
    return tenders


def load_existing(path=None):
    """讀舊 data.json，回傳 {(org, id): tender}。讀不到回傳空 dict。"""
    path = path or OUTPUT_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
        return {tender_key(t): t for t in old.get("tenders", [])}
    except Exception:
        return {}


def history_recently_checked(old, today, days=HISTORY_RECHECK_DAYS):
    """舊資料記錄「查過但查無」的日期，days 天內不重查。"""
    checked = (old or {}).get("history_checked_at")
    if not checked:
        return False
    try:
        d = datetime.date.fromisoformat(checked)
    except ValueError:
        return False
    return 0 <= (today - d).days < days


def plan_history_queries(activity_list, existing, today=None):
    """決定哪些活動案要查歷史。回傳 (需查詢清單, 略過清單)。

    舊資料已有 history 的直接沿用；查過但查無的 7 天內不重查。
    沿用時會把舊的 history / history_checked_at 寫回該筆。
    """
    today = today or taipei_today()
    to_query, skipped = [], []
    for t in activity_list:
        old = existing.get(tender_key(t))
        if old and old.get("history"):
            t["history"] = old["history"]
            if old.get("history_checked_at"):
                t["history_checked_at"] = old["history_checked_at"]
            skipped.append(t)
        elif history_recently_checked(old, today):
            t["history"] = []
            t["history_checked_at"] = old["history_checked_at"]
            skipped.append(t)
        else:
            to_query.append(t)
    return to_query, skipped


def parse_roc_date(text):
    """'115/11/26' → date；格式不符回傳 None。"""
    m = re.match(r'^\s*(\d{2,3})/(\d{1,2})/(\d{1,2})', text or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def carry_stale_tenders(failed_orgs, existing, current_keys, today=None):
    """失敗機關沿用舊資料：該機關、截止日還沒過的案子帶進來，加 stale: true。

    current_keys：本次已抓到的 (org, id)，避免重複。
    比對機關：舊資料有 src_org 就用它；沒有就看 org 欄位是否以機關名開頭。
    """
    today = today or taipei_today()
    carried = []
    for t in existing.values():
        src = t.get("src_org")
        if src:
            if src not in failed_orgs:
                continue
        elif not any(t.get("org", "").startswith(o) for o in failed_orgs):
            continue
        if tender_key(t) in current_keys:
            continue
        deadline = parse_roc_date(t.get("deadline"))
        if deadline is None or deadline < today:
            continue
        t = dict(t)
        t["stale"] = True
        carried.append(t)
    return carried


def critical_failed(failed_orgs):
    return [o for o in failed_orgs if o in CRITICAL_ORGS]


def acquire_lock():
    """防撞鎖：拿不到回傳 None（代表另一份爬蟲正在跑）。"""
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


async def main():
    print("=" * 52)
    print("  起行採購監控爬蟲（雲林、彰化、嘉義）")
    print(f"  時間：{datetime.datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 52)

    existing = load_existing()
    all_tenders = {}
    all_orgs = [(region, org) for region, cfg in REGIONS.items() for org in cfg["orgs"]]

    async with async_playwright() as p:
        browser, context = await make_context(p)
        page = await context.new_page()

        report_progress(5, f"查詢 {len(all_orgs)} 個機關...")
        print(f"\n查詢 {len(all_orgs)} 個機關（等標期內勞務標案）...")

        failed_orgs = []
        successful_org_count = 0
        consecutive_failures = 0
        for i, (region, org) in enumerate(all_orgs):
            print(f"  [{i+1:02d}/{len(all_orgs)}] [{region}] {org}...", end=" ", flush=True)
            try:
                results = await query_org_with_retry(page, org, region)
            except PccBlockedError as exc:
                report_progress(0, "政府採購網封鎖連線；保留上一份正常資料")
                raise RuntimeError(f"{exc}；已停止本次更新並保留舊資料") from exc

            if results is None:
                failed_orgs.append(org)
                consecutive_failures += 1
                print("查詢失敗（重試後仍失敗）")
                report_progress(5 + int((i + 1) / len(all_orgs) * 50), f"查詢 {org} 失敗（{i+1}/{len(all_orgs)}）")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    report_progress(0, "連續查詢失敗；保留上一份正常資料")
                    raise RuntimeError(
                        f"連續 {consecutive_failures} 個機關查詢失敗，"
                        "疑似 PCC 封鎖或版型變更；已停止並保留舊資料"
                    )
                await asyncio.sleep(2)
                continue

            successful_org_count += 1
            consecutive_failures = 0

            original_count = len(results)
            results = [t for t in results if not is_excluded_tender(t["title"])]
            excluded_count = original_count - len(results)

            for t in results:
                add_tender(all_tenders, t)

            activity_count = sum(1 for t in results if t.get("is_activity"))
            exclude_note = f"，工程/設施排除 {excluded_count} 筆" if excluded_count else ""
            print(f"共 {len(results)} 筆，活動相關 {activity_count} 筆{exclude_note}")
            report_progress(5 + int((i + 1) / len(all_orgs) * 50), f"查詢 {org}（{i+1}/{len(all_orgs)}）")
            await asyncio.sleep(2)

        # 補查：開機剛醒網路未通等暫時性失敗，等一下再試一次
        if failed_orgs:
            print(f"\n{len(failed_orgs)} 個機關查詢失敗，30 秒後補查...")
            await asyncio.sleep(30)
            org_region = {org: region for region, org in all_orgs}
            still_failed = []
            for org in failed_orgs:
                print(f"  [補查] [{org_region[org]}] {org}...", end=" ", flush=True)
                try:
                    results = await query_org_with_retry(page, org, org_region[org])
                except PccBlockedError:
                    results = None
                if results is None:
                    still_failed.append(org)
                    print("仍失敗")
                    continue
                successful_org_count += 1
                results = [t for t in results if not is_excluded_tender(t["title"])]
                for t in results:
                    add_tender(all_tenders, t)
                print(f"共 {len(results)} 筆")
                await asyncio.sleep(2)
            failed_orgs = still_failed

        # 失敗機關沿用舊資料（截止日未過的），標 stale
        if failed_orgs:
            stale = carry_stale_tenders(failed_orgs, existing, set(all_tenders))
            for t in stale:
                add_tender(all_tenders, t)
            if stale:
                print(f"\n失敗機關沿用舊資料 {len(stale)} 筆（標示「昨日資料」）")

        all_list = ensure_unique_ids(list(all_tenders.values()))
        activity_list = [t for t in all_list if t.get("is_activity")]

        to_query, skipped = plan_history_queries(activity_list, existing)
        print(f"\n歷史得標：需查詢 {len(to_query)} 筆／略過 {len(skipped)} 筆")

        if to_query:
            report_progress(60, f"查詢 {len(to_query)} 筆活動標案歷史記錄...")
            print(f"查詢 {len(to_query)} 筆活動標案的歷史得標記錄...")
            for i, t in enumerate(to_query):
                kw = extract_keyword(t["title"])
                print(f"  [{i+1}/{len(to_query)}] {t['title'][:25]}... (keyword: {kw})")
                history = await query_history(page, context, t["title"])
                t["history"] = history
                if not history:
                    t["history_checked_at"] = taipei_today().isoformat()
                if history:
                    for h in history:
                        print(f"    {h['year']}年 → {h['winner']} / {h['award_price']}")
                else:
                    print(f"    查無歷史記錄")
                report_progress(60 + int((i + 1) / len(to_query) * 20), f"查詢歷史記錄（{i+1}/{len(to_query)}）")
                await asyncio.sleep(2)

        await browser.close()

    print(f"\n總計：{len(all_list)} 筆勞務標案，其中 {len(activity_list)} 筆活動相關")
    if failed_orgs:
        print(f"⚠ 以下機關查詢失敗，本次資料不含這些機關：{('、'.join(failed_orgs))}")

    print("\n活動相關標案清單：")
    for t in activity_list:
        print(f"  ★ [{t['region']}·{t['town']}] {t['title']} | 預算 {t['budget']}")

    report_progress(90, "儲存資料...")
    validate_crawl_results(
        total_orgs=len(all_orgs),
        failed_count=len(failed_orgs),
        tender_count=len(all_list),
        existing_count=len(existing),
    )

    output = {
        # 帶時區的台北時間；GitHub Actions 在 UTC 執行，沒帶時區前端會顯示錯 8 小時
        "updated_at": datetime.datetime.now(TAIPEI).isoformat(),
        "total": len(all_list),
        "activity_total": len(activity_list),
        "org_count": len(all_orgs),
        "successful_org_count": successful_org_count,
        "failed_orgs": failed_orgs,
        "stale_count": sum(1 for t in all_list if t.get("stale")),
        "tenders": all_list
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n已儲存 {len(all_list)} 筆至 data.json（活動相關 {len(activity_list)} 筆）")

    report_progress(95, "更新 index.html...")
    import subprocess
    import sys
    build_script = os.path.join(os.path.dirname(__file__), "build_index.py")
    if os.path.exists(build_script):
        result = subprocess.run([sys.executable, build_script], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print("index.html 更新失敗：", result.stderr.strip())

    print("完成！")

    upload_script = os.path.join(os.path.dirname(__file__), "upload_github.py")
    if os.environ.get("PCC_AUTO_PUBLISH") == "1" and os.path.exists(upload_script):
        report_progress(98, "上傳 GitHub Pages...")
        result = subprocess.run([sys.executable, upload_script], capture_output=True, text=True)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        if result.returncode != 0:
            raise SystemExit("GitHub Pages 上傳失敗")
    else:
        report_progress(100, "資料已更新；GitHub 發佈需手動確認")
        print("已略過 GitHub push。確認內容後再手動執行 upload_github.py。")

    critical = critical_failed(failed_orgs)
    if critical:
        print(f"⚠ 關鍵機關查詢失敗：{'、'.join(critical)}；exit {EXIT_CRITICAL_FAILED}，等排程重跑")
        return EXIT_CRITICAL_FAILED
    return 0


if __name__ == "__main__":
    lock = acquire_lock()
    if lock is None:
        print("另一份爬蟲正在執行（.crawler.lock 被占用），本次略過")
        raise SystemExit(0)
    try:
        raise SystemExit(asyncio.run(main()))
    finally:
        lock.close()
