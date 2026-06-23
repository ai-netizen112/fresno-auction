"""
Fresno/Madera HiBid Auction Scraper
- Searches hibid.com within 30 miles of 93711 to auto-discover ALL auctions
- Also scrapes known local regulars as backup
- Enriches with Claude AI (normalized titles + smart search tags)
- Saves data.json to repo root; GitHub Actions commits and pushes

Requirements: pip install playwright anthropic
              playwright install chromium
"""

import json, time, re, os
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATA_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
ZIP_CODE = "93711"
RADIUS_MILES = 30

KNOWN_SOURCES = [
    {"id": "70772",  "name": "Public Auctions R Us",    "subdomain": "publicauctionrus",    "location": "Madera, CA",  "pickup": "Mon–Wed 8am–6pm",          "color": "#1e40af", "miles": 18},
    {"id": "138126", "name": "Jaxon's Dealz",           "subdomain": "jaxonsdealz",         "location": "Fresno, CA",  "pickup": "Sat 9am–4pm / Sun 8am–2pm","color": "#065f46", "miles": 8},
    {"id": "87541",  "name": "Auction House of Fresno", "subdomain": "auctionhouse",        "location": "Clovis, CA",  "pickup": "Check listing",             "color": "#7c2d12", "miles": 10},
    {"id": "87866",  "name": "Fresno Auction Company",  "subdomain": "fresnoauctioncompany","location": "Fresno, CA",  "pickup": "Check listing",             "color": "#b45309", "miles": 5},
    {"id": "14506",  "name": "Custom Auction Service",  "subdomain": "customauctionservice","location": "Visalia, CA", "pickup": "Check listing",             "color": "#4c1d95", "miles": 45},
]

CATEGORY_LIST = [
    "Tools & Hardware","Electronics","Furniture","Appliances","Home Goods & Decor",
    "Clothing & Accessories","Toys & Games","Jewelry & Watches","Outdoor & Garden",
    "Sports & Fitness","Automotive","Books & Media","Art & Collectibles","Collectibles","Other"
]

# ── DISCOVERY: find all auctions near zip code ────────────────────────────────

def discover_auctions(page):
    """Search HiBid by zip radius to find all active local auctions."""
    discovered = []
    print(f"\nDiscovering auctions within {RADIUS_MILES} miles of {ZIP_CODE}...")

    try:
        url = f"https://hibid.com/auctions?zip={ZIP_CODE}&miles={RADIUS_MILES}&status=open"
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(3)

        # Scroll to load all results
        for _ in range(5):
            page.keyboard.press("End")
            time.sleep(0.8)

        # Extract auction cards
        for sel in [".auction-card", ".catalog-card", "[class*='auction-item']", "[class*='AuctionCard']", "article"]:
            cards = page.query_selector_all(sel)
            if cards:
                print(f"  Found {len(cards)} auction cards with selector: {sel}")
                for card in cards:
                    try:
                        text = card.inner_text()
                        # Get link
                        a = card.query_selector("a")
                        href = a.get_attribute("href") if a else ""
                        if href and href.startswith("/"):
                            href = "https://hibid.com" + href

                        # Get name
                        name = ""
                        for ns in ["h2","h3","h4",".auction-title","[class*='title']"]:
                            el = card.query_selector(ns)
                            if el:
                                name = el.inner_text().strip()
                                if name: break

                        # Get location/company
                        location = ""
                        loc_match = re.search(r'([A-Za-z\s]+,\s*CA)', text)
                        if loc_match:
                            location = loc_match.group(1).strip()

                        # Get closing date
                        closing = ""
                        for pat in [r'(\d{1,2}/\d{1,2}/\d{2,4}[^|\n]{0,20})', r'([A-Za-z]+ \d{1,2}[^|\n]{0,20})']:
                            m = re.search(pat, text)
                            if m:
                                closing = m.group(1).strip()
                                break

                        if href and name:
                            discovered.append({
                                "url": href,
                                "name": name,
                                "location": location or "Fresno Area, CA",
                                "closing": closing,
                                "color": "#64748b",  # default gray for discovered
                                "miles": None,
                            })
                    except:
                        pass
                break

        # Fallback: grab all catalog/auction links from page
        if not discovered:
            links = page.query_selector_all("a[href*='/catalog/'], a[href*='/auction/']")
            seen = set()
            for link in links:
                href = link.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://hibid.com" + href
                if href not in seen and ("/catalog/" in href or "/auction/" in href):
                    seen.add(href)
                    name = link.inner_text().strip() or href
                    discovered.append({
                        "url": href, "name": name[:80],
                        "location": "Fresno Area, CA",
                        "closing": "", "color": "#64748b", "miles": None,
                    })

    except Exception as e:
        print(f"  Discovery error: {e}")

    print(f"  Discovered {len(discovered)} auctions from zip search")
    return discovered


# ── SCRAPE ONE CATALOG PAGE ───────────────────────────────────────────────────

def scrape_catalog(page, catalog_url, source_name, source_color, location, pickup, miles):
    """Scrape all lots from one catalog/auction page."""
    lots = []
    print(f"  Scraping: {catalog_url[:80]}...")

    try:
        page.goto(catalog_url, wait_until="networkidle", timeout=30000)
        time.sleep(3)
    except Exception as e:
        print(f"    Load error: {e}")
        return lots

    # Get auction title
    auction_title = source_name
    try:
        h = page.query_selector("h1, .auction-title, .catalog-title, [class*='AuctionTitle']")
        if h:
            t = h.inner_text().strip()
            if t: auction_title = t
    except: pass

    # Get closing time from page header (most reliable)
    page_closing = ""
    try:
        page_text = page.inner_text("body")
        for pat in [
            r'(?:Closes?|Ends?|Bidding Ends?)\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*[APap][Mm])',
            r'(?:Closes?|Ends?|Bidding Ends?)\s*:?\s*([A-Za-z]+\.?\s+\d{1,2},?\s*\d{0,4}\s*\d{1,2}:\d{2}\s*[APap][Mm])',
            r'Date[s\(].*?(\d{1,2}/\d{1,2}/\d{4})',
        ]:
            m = re.search(pat, page_text[:3000], re.IGNORECASE)
            if m:
                page_closing = m.group(1).strip()
                break
    except: pass

    # Scroll to load all lots
    for _ in range(10):
        page.keyboard.press("End")
        time.sleep(0.6)

    # Try lot card selectors
    lot_elements = []
    for sel in [".lot-card", ".catalog-lot", "[class*='lot-item']", "[class*='LotCard']",
                "[class*='lot_card']", ".item-card", "[data-lot-id]", ".grid-item",
                "[class*='card']:has(img)", "li:has([class*='bid'])"]:
        els = page.query_selector_all(sel)
        if len(els) > 3:
            lot_elements = els
            print(f"    {len(els)} lots found with: {sel}")
            break

    # Last resort: find repeated structures with bid text
    if not lot_elements:
        lot_elements = page.query_selector_all("div:has(> img):has([class*='bid']), li:has(img)")
        print(f"    Fallback: {len(lot_elements)} elements")

    source_id = re.sub(r'[^a-z0-9]', '_', source_name.lower())[:20]

    for i, el in enumerate(lot_elements[:300]):
        try:
            text = el.inner_text()
            if len(text.strip()) < 5: continue

            # Title
            title = ""
            for ts in ["h3","h2","h4",".lot-title",".item-title","[class*='title']","strong","p"]:
                t = el.query_selector(ts)
                if t:
                    v = t.inner_text().strip()
                    if len(v) > 5:
                        title = v
                        break
            if not title:
                lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 5]
                title = lines[0] if lines else ""
            if not title: continue

            # Lot number
            lot_num = ""
            m = re.search(r'[Ll]ot\s*#?\s*(\d+)', text)
            if m: lot_num = m.group(1)

            # Current bid
            current_bid = 0.0
            bids = re.findall(r'\$\s*([\d,]+\.?\d*)', text)
            if bids:
                try: current_bid = float(bids[0].replace(",",""))
                except: pass

            # Bid count
            bid_count = 0
            bc = re.search(r'(\d+)\s+[Bb]id', text)
            if bc:
                try: bid_count = int(bc.group(1))
                except: pass

            # Closing time — prefer per-lot time, fallback to page-level
            closing_time = page_closing
            for pat in [
                r'(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*[APap][Mm])',
                r'(\d+d\s+\d+h\s+\d+m)',   # e.g. "2d 13h 41m"
                r'([A-Za-z]+\.?\s+\d{1,2},?\s*\d{4}\s+\d{1,2}:\d{2}\s*[APap][Mm])',
            ]:
                lm = re.search(pat, text, re.IGNORECASE)
                if lm:
                    closing_time = lm.group(1).strip()
                    break

            # Image
            image_url = ""
            img = el.query_selector("img")
            if img:
                image_url = img.get_attribute("src") or img.get_attribute("data-src") or ""
                if image_url and image_url.startswith("/"): image_url = "https://hibid.com" + image_url

            # Lot URL
            lot_url = catalog_url
            a = el.query_selector("a")
            if a:
                href = a.get_attribute("href") or ""
                if href.startswith("/"): lot_url = "https://hibid.com" + href
                elif href.startswith("http"): lot_url = href

            lots.append({
                "id": f"{source_id}_{lot_num or i}_{int(time.time()*1000) % 99999}",
                "lotNumber": lot_num or str(i+1),
                "title": title,
                "rawTitle": title,
                "normalizedTitle": "",
                "description": "",
                "currentBid": current_bid,
                "bidCount": bid_count,
                "closingTime": closing_time,
                "imageUrl": image_url,
                "url": lot_url,
                "auctionName": auction_title,
                "sourceId": source_id,
                "sourceName": source_name,
                "sourceColor": source_color,
                "location": location,
                "pickup": pickup,
                "miles": miles,
                "category": "",
                "tags": [],
                "isSteal": False,
                "scrapedAt": datetime.now(timezone.utc).isoformat(),
            })
        except: pass

    print(f"    Extracted {len(lots)} lots")
    return lots


# ── AI ENRICHMENT ─────────────────────────────────────────────────────────────

def enrich_lots_with_ai(lots):
    if not ANTHROPIC_API_KEY:
        print("No API key — skipping AI enrichment")
        for lot in lots:
            lot["normalizedTitle"] = lot["rawTitle"]
            lot["category"] = "Other"
        return lots

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    BATCH = 30
    print(f"\nEnriching {len(lots)} lots with Claude AI...")

    for i in range(0, len(lots), BATCH):
        batch = lots[i:i+BATCH]
        titles = [{"index": j, "title": lot["rawTitle"]} for j, lot in enumerate(batch)]
        prompt = f"""You are organizing auction listings for a family in Fresno, CA.
For each lot title, return a JSON array with:
- index: (same number)
- normalizedTitle: clean readable name (e.g. "Nail Gun" not "BOSTITCH 15GA FINISH NAILER")
- category: one of {json.dumps(CATEGORY_LIST)}
- tags: 6-12 search terms a regular person might use — synonyms, type, brand, use case
- isSteal: true if it sounds like a great deal for a common useful item

Input: {json.dumps(titles)}
Return ONLY valid JSON array, no markdown."""

        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role":"user","content":prompt}]
            )
            raw = r.content[0].text.strip().replace("```json","").replace("```","").strip()
            results = json.loads(raw)
            for res in results:
                idx = res.get("index", 0)
                if idx < len(batch):
                    batch[idx]["normalizedTitle"] = res.get("normalizedTitle", batch[idx]["rawTitle"])
                    batch[idx]["category"] = res.get("category", "Other")
                    batch[idx]["tags"] = res.get("tags", [])
                    batch[idx]["isSteal"] = res.get("isSteal", False)
        except Exception as e:
            print(f"  AI batch error: {e}")
            for lot in batch:
                lot["normalizedTitle"] = lot["rawTitle"]
                lot["category"] = "Other"

        print(f"  Enriched {min(i+BATCH, len(lots))}/{len(lots)}")
        time.sleep(0.5)

    return lots


# ── STEAL DETECTION ───────────────────────────────────────────────────────────

def flag_steals(lots):
    for lot in lots:
        if lot["bidCount"] == 0 and lot["currentBid"] <= 10.0:
            lot["isSteal"] = True
        if lot.get("isSteal") and lot["currentBid"] > 30:
            lot["isSteal"] = False
    return lots


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("="*60)
    print(f"Fresno Auction Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)

    all_lots = []
    all_sources = []
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900}
        )
        page = ctx.new_page()

        # ── Step 1: Zip radius discovery ──────────────────────────────────────
        discovered = discover_auctions(page)
        for auction in discovered:
            url = auction["url"]
            if url in seen_urls: continue
            seen_urls.add(url)
            lots = scrape_catalog(
                page, url,
                auction["name"], auction["color"],
                auction["location"], "Check listing", auction["miles"]
            )
            if lots:
                all_lots.extend(lots)
                all_sources.append({
                    "id": re.sub(r'[^a-z0-9]','_', auction["name"].lower())[:20],
                    "name": auction["name"],
                    "color": auction["color"],
                    "location": auction["location"],
                    "pickup": "Check listing",
                    "miles": auction["miles"],
                    "url": url,
                })
            time.sleep(1)

        # ── Step 2: Known regulars (fallback + supplement) ────────────────────
        for src in KNOWN_SOURCES:
            base_url = f"https://{src['subdomain']}.hibid.com"
            if base_url in seen_urls: continue

            print(f"\n[{src['name']}] {base_url}")
            try:
                page.goto(base_url, wait_until="networkidle", timeout=30000)
                time.sleep(2)

                # Find catalog links
                catalog_links = set()
                for a in page.query_selector_all("a[href*='/catalog/'], a[href*='/auction/']"):
                    href = a.get_attribute("href") or ""
                    if href.startswith("/"): href = base_url + href
                    if href: catalog_links.add(href)
                if not catalog_links: catalog_links.add(base_url)

                src_lots = []
                for cat_url in list(catalog_links)[:5]:
                    if cat_url in seen_urls: continue
                    seen_urls.add(cat_url)
                    lots = scrape_catalog(page, cat_url, src["name"], src["color"], src["location"], src["pickup"], src["miles"])
                    src_lots.extend(lots)
                    time.sleep(1)

                if src_lots:
                    all_lots.extend(src_lots)
                    all_sources.append({
                        "id": src["id"], "name": src["name"], "color": src["color"],
                        "location": src["location"], "pickup": src["pickup"],
                        "miles": src["miles"], "url": base_url,
                    })
                    print(f"  ✓ {src['name']}: {len(src_lots)} lots")
                else:
                    print(f"  — {src['name']}: no active lots")

            except Exception as e:
                print(f"  ✗ {src['name']}: {e}")

        browser.close()

    print(f"\nTotal lots scraped: {len(all_lots)}")

    # Deduplicate by title+source
    seen_ids = set()
    unique_lots = []
    for lot in all_lots:
        key = f"{lot['sourceName']}_{lot['rawTitle'][:40]}"
        if key not in seen_ids:
            seen_ids.add(key)
            unique_lots.append(lot)
    print(f"After dedup: {len(unique_lots)} lots")

    # Enrich + flag
    unique_lots = enrich_lots_with_ai(unique_lots)
    unique_lots = flag_steals(unique_lots)

    # Deduplicate sources
    seen_src = set()
    unique_sources = []
    for s in all_sources:
        if s["id"] not in seen_src:
            seen_src.add(s["id"])
            unique_sources.append(s)

    output = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "lastUpdatedLocal": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "totalLots": len(unique_lots),
        "sources": unique_sources,
        "lots": unique_lots,
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Saved {len(unique_lots)} lots to {DATA_JSON_PATH}")
    print("Done! GitHub Actions will commit and push.")


if __name__ == "__main__":
    main()
