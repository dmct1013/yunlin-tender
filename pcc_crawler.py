#!/usr/bin/env python3
"""
中彰雲嘉南活動採購案爬蟲（雲林、台中、彰化、嘉義、台南）
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
from playwright.async_api import async_playwright

try:
    from zoneinfo import ZoneInfo
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:
    # Windows 可能沒裝 tzdata；台灣無夏令時間，固定 +08:00 等價
    TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "data.json")
STATUS_FILE = os.path.join(os.path.dirname(__file__), ".status.json")


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

ACTIVITY_KEYWORDS = [
    "表演", "演出", "演唱", "舞蹈", "戲劇", "展覽", "特展", "策展", "裝置",
    "活動", "節慶", "慶典", "祭典", "嘉年華", "晚會", "博覽會", "園遊會",
    "運動會", "競賽", "競技", "馬拉松", "論壇", "頒獎",
    "行銷", "宣傳", "推廣", "宣導",
    "文化", "藝術", "音樂", "交流", "藝文",
    "社區", "社造", "社區營造", "培力", "地方創生",
    "文史", "口述", "人文保存", "眷村", "部落",
    "古蹟日", "燈會", "市集", "導覽", "工作坊", "研習", "講座", "展售",
    "燈籠", "彩繪",
    "農村再生", "社區規劃", "關懷據點", "樂齡", "長者", "共餐", "志工",
    "食農", "環境教育", "客庄", "客家", "原住民",
]

# 標題裡的機關名／路名本身含「文化」等字，比對前先剔除，避免誤判成活動案
ORG_NOISE = ["文化觀光處", "文化處", "文化局", "文化路"]

# 標題含這些詞的多半是工程設計監造案，不是活動標案
EXCLUDE_KEYWORDS = [
    "監造", "修繕", "汰換", "新建工程", "改善工程", "改建工程", "開闢工程",
    "道路工程", "公園工程", "排水工程", "停車場",
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

# 各區域的監控機關；直轄市（台中、台南）用市政府前綴涵蓋所有局處，
# 彰化、嘉義依 DAVID 指示連鄉鎮市公所一起監控（2026-07-04）
REGIONS = {
    "雲林": {
        "orgs": ["雲林縣政府"] + [t + "公所" for t in YUNLIN_TOWNS],
        "towns": YUNLIN_TOWNS,
        "fallback": "雲林縣",
    },
    "台中": {"orgs": ["臺中市政府"], "towns": [], "fallback": "台中市"},
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
    "台南": {"orgs": ["臺南市政府"], "towns": [], "fallback": "台南市"},
}

_PLACE_WORDS = sorted(
    set(
        ["雲林縣政府", "雲林縣", "臺中市政府", "臺中市", "台中市",
         "彰化縣政府", "彰化縣", "嘉義縣政府", "嘉義市政府", "嘉義縣", "嘉義市",
         "臺南市政府", "臺南市", "台南市"]
        + YUNLIN_TOWNS + CHANGHUA_TOWNS + CHIAYI_TOWNS
    ),
    key=len, reverse=True,
)
TOWNS_PATTERN = r'^(' + '|'.join(_PLACE_WORDS) + r')'


def is_activity(title):
    t = title
    for w in ORG_NOISE:
        t = t.replace(w, "")
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in t for kw in ACTIVITY_KEYWORDS)


def town_from_org(org, region):
    cfg = REGIONS[region]
    for t in cfg["towns"]:
        if t in org:
            return t
    return cfg["fallback"]


# 「第二十二屆」這類屆次詞每年都變，且會讓縮短後的關鍵字撈到不相干的案子
ORDINAL_RE = r'第[0-9一二三四五六七八九十百]+屆'


def extract_keyword(title):
    """從標案名稱提取搜尋關鍵字"""
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
    t = re.sub(r'^[0-9A-Za-z\-]+\s+', '', title).strip()  # 決標列表的標案編號前綴
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


async def query_history(page, context, title, years=None):
    """用全文檢索查詢前兩年歷史得標。先用 6 字關鍵字查，查無時退用 4 字再查一次。"""
    if years is None:
        today = datetime.date.today()
        roc_year = today.year - 1911
        years = [roc_year - 1, roc_year - 2]

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




async def make_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=['--disable-blink-features=AutomationControlled']
    )
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
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
        await page.wait_for_timeout(8000)
        await page.wait_for_selector('input[name="orgName"]', timeout=10000)

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
                })
            except Exception:
                continue

    except Exception as e:
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


async def main():
    print("=" * 52)
    print("  中彰雲嘉南採購案爬蟲")
    print(f"  時間：{datetime.datetime.now(TAIPEI).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 52)

    all_tenders = {}
    all_orgs = [(region, org) for region, cfg in REGIONS.items() for org in cfg["orgs"]]

    async with async_playwright() as p:
        browser, context = await make_context(p)
        page = await context.new_page()

        report_progress(5, f"查詢 {len(all_orgs)} 個機關...")
        print(f"\n查詢 {len(all_orgs)} 個機關（等標期內勞務標案）...")

        failed_orgs = []
        for i, (region, org) in enumerate(all_orgs):
            print(f"  [{i+1:02d}/{len(all_orgs)}] [{region}] {org}...", end=" ", flush=True)
            results = await query_org_with_retry(page, org, region)

            if results is None:
                failed_orgs.append(org)
                print("查詢失敗（重試後仍失敗）")
                report_progress(5 + int((i + 1) / len(all_orgs) * 50), f"查詢 {org} 失敗（{i+1}/{len(all_orgs)}）")
                await asyncio.sleep(2)
                continue

            for t in results:
                if t["id"] not in all_tenders:
                    all_tenders[t["id"]] = t

            activity_count = sum(1 for t in results if t.get("is_activity"))
            print(f"共 {len(results)} 筆，活動相關 {activity_count} 筆")
            report_progress(5 + int((i + 1) / len(all_orgs) * 50), f"查詢 {org}（{i+1}/{len(all_orgs)}）")
            await asyncio.sleep(2)

        all_list = list(all_tenders.values())
        activity_list = [t for t in all_list if t.get("is_activity")]

        if activity_list:
            report_progress(60, f"查詢 {len(activity_list)} 筆活動標案歷史記錄...")
            print(f"\n查詢 {len(activity_list)} 筆活動標案的歷史得標記錄...")
            for i, t in enumerate(activity_list):
                kw = extract_keyword(t["title"])
                print(f"  [{i+1}/{len(activity_list)}] {t['title'][:25]}... (keyword: {kw})")
                history = await query_history(page, context, t["title"])
                t["history"] = history
                if history:
                    for h in history:
                        print(f"    {h['year']}年 → {h['winner']} / {h['award_price']}")
                else:
                    print(f"    查無歷史記錄")
                report_progress(60 + int((i + 1) / len(activity_list) * 20), f"查詢歷史記錄（{i+1}/{len(activity_list)}）")
                await asyncio.sleep(2)

        await browser.close()

    print(f"\n總計：{len(all_list)} 筆勞務標案，其中 {len(activity_list)} 筆活動相關")
    if failed_orgs:
        print(f"⚠ 以下機關查詢失敗，本次資料不含這些機關：{('、'.join(failed_orgs))}")

    print("\n活動相關標案清單：")
    for t in activity_list:
        print(f"  ★ [{t['region']}·{t['town']}] {t['title']} | 預算 {t['budget']}")

    report_progress(90, "儲存資料...")
    existing = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            existing = {t["id"]: t for t in old.get("tenders", [])}
        except Exception:
            pass

    for t in all_list:
        if t["id"] in existing and existing[t["id"]].get("history"):
            if not t.get("history"):
                t["history"] = existing[t["id"]]["history"]

    output = {
        # 帶時區的台北時間；GitHub Actions 在 UTC 執行，沒帶時區前端會顯示錯 8 小時
        "updated_at": datetime.datetime.now(TAIPEI).isoformat(),
        "total": len(all_list),
        "activity_total": len(activity_list),
        "failed_orgs": failed_orgs,
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

    report_progress(98, "上傳 GitHub Pages...")
    upload_script = os.path.join(os.path.dirname(__file__), "upload_github.py")
    if os.path.exists(upload_script):
        result = subprocess.run([sys.executable, upload_script], capture_output=True, text=True)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        if result.returncode != 0:
            raise SystemExit("GitHub Pages 上傳失敗")


if __name__ == "__main__":
    asyncio.run(main())
