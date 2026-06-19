"""
Fresno/Madera HiBid Auction Scraper
Runs via GitHub Actions on Thu/Fri/Sat at 2am Pacific.
Scrapes all active local auctions, enriches with Claude AI, saves data.json.

Requirements: pip install playwright anthropic
              playwright install chromium
"""

import json
import time
import re
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
# API key comes from GitHub Secret (set in repo Settings → Secrets)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# data.json lives in repo root (served by GitHub Pages from root)
DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "data.json")

# HiBid company IDs for Fresno/Madera area auctions
# Add or remove as you discover new local ones
AUCTION_SOURCES = [
    {
        "id": "70772",
        "name": "Public Auctions R Us",
        "subdomain": "publicauctionrus",
        "location": "Madera, CA",
        "pickup": "Mon–Wed 8am–6pm",
        "color": "#1e40af",
    },
    {
        "id": "138126",
        "name": "Jaxon's Dealz",
        "subdomain": "jaxonsdealz",
        "location": "Fresno, CA",
        "pickup": "Sat 9am–4pm / Sun 8am–2pm",
        "color": "#065f46",
    },
    {
        "id": "auctionhouse",   # Auction House Fresno
        "name": "Auction House Fresno",
        "subdomain": "auctionhouse",
        "location": "Fresno, CA",
        "pickup": "Check listing",
        "color": "#7c2d12",
    },
    {
        "id": "warehouse15",
        "name": "Warehouse 15 Fresno",
        "subdomain": "warehouse15fresno",
        "location": "Fresno, CA",
        "pickup": "Fri–Sun 9am–4pm",
        "color": "#4c1d95",
    },
]

CATEGORY_LIST = [
    "Tools & Hardware", "Electronics", "Furniture", "Appliances",
    "Home Goods & Decor", "Clothing & Accessories", "Toys & Games",
    "Jewelry & Watches", "Outdoor & Garden", "Sports & Fitness",
    "Automotive", "Books & Media", "Food & Grocery", "Art & Collectibles", "Other"
]

# ── SCRAPER ───────────────────────────────────────────────────────────────────

def scrape_auction(page, source):
    """Scrape all active lots from one HiBid auction company."""
    lots = []
    base_url = f"https://{source['subdomain']}.hibid.com"

    print(f"\n[{source['name']}] Loading {base_url}...")
    try:
        page.goto(base_url, wait_until="networkidle", timeout=30000)
        time.sleep(2)
    except Exception as e:
        print(f"  ERROR loading page: {e}")
        return lots

    # Find active auction catalog links
    catalog_links = set()
    anchors = page.query_selector_all("a[href*='/catalog/'], a[href*='/auction/']")
    for a in anchors:
        href = a.get_attribute("href")
        if href:
            if href.startswith("/"):
                href = base_url + href
            catalog_links.add(href)

    # Also check for direct lot listings on the main page
    if not catalog_links:
        catalog_links.add(base_url)

    print(f"  Found {len(catalog_links)} catalog(s): {list(catalog_links)[:3]}")

    for catalog_url in list(catalog_links)[:5]:  # limit to 5 catalogs per source
        lots.extend(scrape_catalog(page, catalog_url, source))
        time.sleep(1)

    return lots


def scrape_catalog(page, catalog_url, source):
    """Scrape individual lots from one catalog/auction page."""
    lots = []
    print(f"  Scraping catalog: {catalog_url}")

    try:
        page.goto(catalog_url, wait_until="networkidle", timeout=30000)
        time.sleep(3)
    except Exception as e:
        print(f"    ERROR: {e}")
        return lots

    # Extract auction name/date from page
    auction_title = ""
    try:
        h1 = page.query_selector("h1, .auction-title, .catalog-title")
        if h1:
            auction_title = h1.inner_text().strip()
    except:
        pass

    # Scroll to load all lots (HiBid lazy loads)
    for _ in range(8):
        page.keyboard.press("End")
        time.sleep(0.8)

    # Try multiple selectors HiBid uses for lot cards
    lot_selectors = [
        ".lot-card",
        ".catalog-lot",
        "[class*='lot-item']",
        "[class*='LotCard']",
        ".item-card",
        "[data-lot-id]",
        ".grid-item",
    ]

    lot_elements = []
    for sel in lot_selectors:
        els = page.query_selector_all(sel)
        if els:
            lot_elements = els
            print(f"    Found {len(els)} lots with selector: {sel}")
            break

    if not lot_elements:
        # Fallback: look for any repeated card-like structure with bid info
        lot_elements = page.query_selector_all("[class*='card']:has([class*='bid']), [class*='lot']:has(img)")
        print(f"    Fallback selector found {len(lot_elements)} elements")

    for i, el in enumerate(lot_elements[:200]):  # cap at 200 lots
        try:
            lot = extract_lot_data(el, source, catalog_url, auction_title, i)
            if lot and lot.get("title"):
                lots.append(lot)
        except Exception as e:
            pass  # skip malformed lots silently

    print(f"    Extracted {len(lots)} lots")
    return lots


def extract_lot_data(el, source, catalog_url, auction_title, index):
    """Pull raw data from a lot card element."""
    text = el.inner_text()
    html = el.inner_html()

    # Title
    title = ""
    for sel in ["h3", "h2", ".lot-title", ".item-title", "[class*='title']", "strong"]:
        try:
            t = el.query_selector(sel)
            if t:
                title = t.inner_text().strip()
                if len(title) > 5:
                    break
        except:
            pass
    if not title:
        # grab first substantial text line
        lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 8]
        title = lines[0] if lines else ""

    # Lot number
    lot_num = ""
    m = re.search(r'[Ll]ot\s*#?\s*(\d+)', text)
    if m:
        lot_num = m.group(1)

    # Current bid
    current_bid = 0.0
    bid_match = re.search(r'\$\s*([\d,]+\.?\d*)', text)
    if bid_match:
        try:
            current_bid = float(bid_match.group(1).replace(",", ""))
        except:
            pass

    # Bid count
    bid_count = 0
    bc_match = re.search(r'(\d+)\s+[Bb]id', text)
    if bc_match:
        try:
            bid_count = int(bc_match.group(1))
        except:
            pass

    # Closing time
    closing_time = ""
    for pattern in [
        r'Closes?\s*:?\s*([A-Za-z]+\.?\s+\d+[^|\n]{0,30})',
        r'Ends?\s*:?\s*([A-Za-z]+\.?\s+\d+[^|\n]{0,30})',
        r'(\d{1,2}/\d{1,2}/\d{2,4}[^|\n]{0,20})',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            closing_time = m.group(1).strip()
            break

    # Image URL
    image_url = ""
    try:
        img = el.query_selector("img")
        if img:
            image_url = img.get_attribute("src") or img.get_attribute("data-src") or ""
    except:
        pass

    # Lot link
    lot_url = catalog_url
    try:
        a = el.query_selector("a[href*='/lot/'], a[href*='/item/'], a")
        if a:
            href = a.get_attribute("href") or ""
            if href.startswith("/"):
                base = f"https://{source['subdomain']}.hibid.com"
                lot_url = base + href
            elif href.startswith("http"):
                lot_url = href
    except:
        pass

    return {
        "id": f"{source['id']}_{lot_num or index}_{int(time.time())}",
        "lotNumber": lot_num or str(index + 1),
        "title": title,
        "rawTitle": title,
        "description": "",
        "currentBid": current_bid,
        "bidCount": bid_count,
        "closingTime": closing_time,
        "imageUrl": image_url,
        "url": lot_url,
        "auctionName": auction_title or source["name"],
        "sourceId": source["id"],
        "sourceName": source["name"],
        "sourceColor": source["color"],
        "location": source["location"],
        "pickup": source["pickup"],
        "category": "",
        "tags": [],
        "normalizedTitle": "",
        "isSteal": False,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
    }


# ── AI ENRICHMENT ─────────────────────────────────────────────────────────────

def enrich_lots_with_ai(lots, api_key):
    """Send lots to Claude in batches to normalize titles and add search tags."""
    try:
        import anthropic
    except ImportError:
        print("anthropic not installed — skipping AI enrichment")
        return lots

    client = anthropic.Anthropic(api_key=api_key)
    BATCH_SIZE = 30
    enriched = []

    print(f"\nEnriching {len(lots)} lots with Claude AI...")

    for i in range(0, len(lots), BATCH_SIZE):
        batch = lots[i:i + BATCH_SIZE]
        titles = [{"index": j, "title": lot["rawTitle"]} for j, lot in enumerate(batch)]

        prompt = f"""You are organizing auction lot listings for a family in Fresno, CA.
For each lot title below, return a JSON array with one object per lot containing:
- index: (same as input)
- normalizedTitle: clean readable name (e.g. "Nail Gun" not "BOSTITCH 15GA FINISH NAILER W/CASE")
- category: one of {json.dumps(CATEGORY_LIST)}
- tags: array of 5-10 search terms someone might use to find this item (synonyms, related terms, brand, type). Think about what words a regular person would search — if it's a nail gun also include "nailer", "framing", "pneumatic", "carpentry". If it's a TV also include "television", "flatscreen", "screen".
- isSteal: true if this sounds like a great deal (common useful item, tools, electronics, furniture, appliances)

Input lots:
{json.dumps(titles, indent=2)}

Return ONLY a valid JSON array. No markdown, no explanation."""

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
            results = json.loads(raw)

            for result in results:
                idx = result.get("index", 0)
                if idx < len(batch):
                    lot = batch[idx]
                    lot["normalizedTitle"] = result.get("normalizedTitle", lot["rawTitle"])
                    lot["category"] = result.get("category", "Other")
                    lot["tags"] = result.get("tags", [])
                    lot["isSteal"] = result.get("isSteal", False)

        except Exception as e:
            print(f"  AI batch {i//BATCH_SIZE + 1} error: {e}")
            # fallback: use raw title
            for lot in batch:
                lot["normalizedTitle"] = lot["rawTitle"]
                lot["category"] = "Other"

        enriched.extend(batch)
        print(f"  Enriched {min(i + BATCH_SIZE, len(lots))}/{len(lots)} lots...")
        time.sleep(1)

    return enriched


# ── STEAL DETECTION ───────────────────────────────────────────────────────────

def flag_steals(lots):
    """Flag no-bid lots under $10 as steals."""
    for lot in lots:
        if lot["bidCount"] == 0 and lot["currentBid"] <= 10.0:
            lot["isSteal"] = True
        # Also flag if AI said it's a steal AND bid is low
        if lot.get("isSteal") and lot["currentBid"] > 25:
            lot["isSteal"] = False  # not a steal if bid is already high
    return lots


# ── GIT COMMIT handled by GitHub Actions workflow ─────────────────────────────


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    from playwright.sync_api import sync_playwright

    print("=" * 60)
    print("Fresno/Madera Auction Scraper")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    all_lots = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for source in AUCTION_SOURCES:
            try:
                lots = scrape_auction(page, source)
                all_lots.extend(lots)
                print(f"  ✓ {source['name']}: {len(lots)} lots")
            except Exception as e:
                print(f"  ✗ {source['name']}: {e}")

        browser.close()

    print(f"\nTotal raw lots scraped: {len(all_lots)}")

    # AI enrichment
    if all_lots and ANTHROPIC_API_KEY != "sk-ant-REPLACE_ME":
        all_lots = enrich_lots_with_ai(all_lots, ANTHROPIC_API_KEY)
    else:
        print("Skipping AI enrichment (no API key set or no lots found)")
        for lot in all_lots:
            lot["normalizedTitle"] = lot["rawTitle"]
            lot["category"] = "Other"
            lot["tags"] = []

    # Flag steals
    all_lots = flag_steals(all_lots)

    # Build final output
    output = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "lastUpdatedLocal": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "totalLots": len(all_lots),
        "sources": [
            {
                "id": s["id"],
                "name": s["name"],
                "color": s["color"],
                "location": s["location"],
                "pickup": s["pickup"],
                "url": f"https://{s['subdomain']}.hibid.com",
            }
            for s in AUCTION_SOURCES
        ],
        "lots": all_lots,
    }

    # Save locally
    os.makedirs(os.path.dirname(DATA_JSON_PATH), exist_ok=True)
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Saved {len(all_lots)} lots to {DATA_JSON_PATH}")
    print("\nDone! GitHub Actions will commit and push.")


if __name__ == "__main__":
    main()
