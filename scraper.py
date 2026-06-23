"""
Fresno/Madera HiBid Auction Scraper
Uses direct HTTP requests to HiBid's internal JSON API (no browser needed).
Runs via GitHub Actions Thu/Fri/Sat at 2am Pacific.
"""

import json, time, re, os, requests
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATA_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")

# HiBid internal API base
API_BASE = "https://hibid.com"
ZIP_CODE = "93711"
RADIUS   = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://hibid.com/",
    "Origin": "https://hibid.com",
}

KNOWN_COMPANY_IDS = [
    {"id": "70772",  "name": "Public Auctions R Us",    "location": "Madera, CA",  "pickup": "Mon–Wed 8am–6pm",          "color": "#1e40af", "miles": 18},
    {"id": "138126", "name": "Jaxon's Dealz",           "location": "Fresno, CA",  "pickup": "Sat 9am–4pm / Sun 8am–2pm","color": "#065f46", "miles": 8},
    {"id": "87541",  "name": "Auction House of Fresno", "location": "Clovis, CA",  "pickup": "Check listing",             "color": "#7c2d12", "miles": 10},
    {"id": "87866",  "name": "Fresno Auction Company",  "location": "Fresno, CA",  "pickup": "Check listing",             "color": "#b45309", "miles": 5},
    {"id": "14506",  "name": "Custom Auction Service",  "location": "Visalia, CA", "pickup": "Check listing",             "color": "#4c1d95", "miles": 45},
]

CATEGORY_LIST = [
    "Tools & Hardware","Electronics","Furniture","Appliances","Home Goods & Decor",
    "Clothing & Accessories","Toys & Games","Jewelry & Watches","Outdoor & Garden",
    "Sports & Fitness","Automotive","Books & Media","Art & Collectibles","Collectibles","Other"
]

session = requests.Session()
session.headers.update(HEADERS)


# ── DISCOVER CATALOGS ─────────────────────────────────────────────────────────

def discover_catalogs():
    """Find all open auction catalogs near zip via HiBid's search API."""
    catalogs = []
    print(f"\nSearching HiBid for auctions within {RADIUS} miles of {ZIP_CODE}...")

    # Try multiple API endpoint patterns HiBid uses
    endpoints = [
        f"{API_BASE}/api/v1/auction/search?zip={ZIP_CODE}&miles={RADIUS}&status=open&page=1&pageSize=100",
        f"{API_BASE}/api/v1/catalog/search?zip={ZIP_CODE}&miles={RADIUS}&status=open&page=1&pageSize=100",
        f"{API_BASE}/api/v1/auctions?zip={ZIP_CODE}&miles={RADIUS}&status=open",
        f"{API_BASE}/catalog-api/v1/search?zip={ZIP_CODE}&miles={RADIUS}&status=open",
        f"https://api.hibid.com/v1/auctions/search?zip={ZIP_CODE}&miles={RADIUS}",
    ]

    for url in endpoints:
        try:
            r = session.get(url, timeout=15)
            print(f"  {url[:80]} → {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                catalogs = parse_catalog_response(data)
                if catalogs:
                    print(f"  ✓ Found {len(catalogs)} catalogs")
                    break
        except Exception as e:
            print(f"  Error: {e}")

    return catalogs


def parse_catalog_response(data):
    """Extract catalog info from various API response formats."""
    catalogs = []

    # Handle list response
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Try common wrapper keys
        for key in ["catalogs","auctions","items","results","data","content"]:
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        else:
            items = [data]
    else:
        return []

    for item in items:
        if not isinstance(item, dict): continue
        catalog_id = (item.get("catalogId") or item.get("id") or item.get("catalog_id") or "")
        name = (item.get("title") or item.get("name") or item.get("auctionTitle") or "Unknown Auction")
        company_id = str(item.get("companyId") or item.get("company_id") or "")
        company_name = (item.get("companyName") or item.get("company_name") or name)
        location = (item.get("city") or item.get("location") or "Fresno Area")
        state = item.get("state","CA")
        closing = (item.get("endDate") or item.get("closingDate") or item.get("end_date") or "")

        if catalog_id:
            catalogs.append({
                "catalogId": str(catalog_id),
                "name": name,
                "companyId": company_id,
                "companyName": company_name,
                "location": f"{location}, {state}",
                "closing": closing,
                "url": f"https://hibid.com/catalog/{catalog_id}",
                "color": "#64748b",
                "miles": None,
            })

    return catalogs


# ── GET LOTS FOR A CATALOG ────────────────────────────────────────────────────

def get_lots_for_catalog(catalog_id, catalog_info):
    """Fetch all lots for a specific catalog via API."""
    lots = []
    page = 1
    page_size = 100

    print(f"  Fetching lots for catalog {catalog_id}...")

    # Try multiple lot API patterns
    lot_endpoints = [
        f"{API_BASE}/api/v1/lots?catalogId={catalog_id}&page={{page}}&pageSize={page_size}",
        f"{API_BASE}/api/v1/catalog/{catalog_id}/lots?page={{page}}&pageSize={page_size}",
        f"{API_BASE}/catalog-api/v1/catalog/{catalog_id}/lots?page={{page}}&pageSize={page_size}",
        f"{API_BASE}/api/v1/auctions/{catalog_id}/lots?page={{page}}&pageSize={page_size}",
    ]

    working_endpoint = None
    for ep_template in lot_endpoints:
        url = ep_template.format(page=1)
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                parsed = parse_lots_response(data, catalog_info)
                if parsed:
                    working_endpoint = ep_template
                    lots.extend(parsed)
                    print(f"    Page 1: {len(parsed)} lots via {url[:60]}")
                    break
        except Exception as e:
            print(f"    Error {url[:50]}: {e}")

    # Paginate if we found a working endpoint
    if working_endpoint and len(lots) >= page_size:
        page = 2
        while True:
            url = working_endpoint.format(page=page)
            try:
                r = session.get(url, timeout=15)
                if r.status_code != 200: break
                data = r.json()
                page_lots = parse_lots_response(data, catalog_info)
                if not page_lots: break
                lots.extend(page_lots)
                print(f"    Page {page}: {len(page_lots)} lots")
                if len(page_lots) < page_size: break
                page += 1
                time.sleep(0.5)
            except: break

    return lots


def parse_lots_response(data, catalog_info):
    """Parse lot data from API response."""
    lots = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ["lots","items","results","data","content","lotList"]:
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        else:
            items = []
    else:
        return []

    for item in items:
        if not isinstance(item, dict): continue

        lot_id   = str(item.get("lotId") or item.get("id") or item.get("lot_id") or "")
        lot_num  = str(item.get("lotNumber") or item.get("lot_number") or item.get("lotNum") or lot_id)
        title    = (item.get("title") or item.get("name") or item.get("description") or "Unknown Item")
        desc     = (item.get("description") or item.get("longDescription") or "")
        if desc == title: desc = ""

        # Bid info
        current_bid = 0.0
        for bk in ["currentBid","current_bid","highBid","high_bid","bidAmount","currentAmount"]:
            v = item.get(bk)
            if v is not None:
                try: current_bid = float(v); break
                except: pass

        bid_count = 0
        for bck in ["bidCount","bid_count","numberOfBids","numBids","bids"]:
            v = item.get(bck)
            if v is not None:
                try: bid_count = int(v); break
                except: pass

        # Closing time
        closing_time = catalog_info.get("closing","")
        for ck in ["endDate","end_date","closingDate","closing_date","closingTime","endTime","bidEndDate"]:
            v = item.get(ck)
            if v:
                closing_time = v
                break

        # Format closing time nicely
        if closing_time:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(closing_time.replace("Z","+00:00"))
                closing_time = dt.strftime("%b %d, %Y %I:%M %p")
            except: pass

        # Image
        image_url = ""
        for ik in ["imageUrl","image_url","thumbnailUrl","thumbnail","primaryImageUrl","imageUrls"]:
            v = item.get(ik)
            if v:
                if isinstance(v, list): v = v[0]
                image_url = str(v)
                break

        # URL
        lot_url = f"https://hibid.com/lot/{lot_id}" if lot_id else catalog_info.get("url","https://hibid.com")

        source_id = re.sub(r'[^a-z0-9]','_', catalog_info.get("companyName","unknown").lower())[:20]

        lots.append({
            "id": f"{source_id}_{lot_id or lot_num}",
            "lotNumber": lot_num,
            "title": title,
            "rawTitle": title,
            "normalizedTitle": "",
            "description": desc[:300] if desc else "",
            "currentBid": current_bid,
            "bidCount": bid_count,
            "closingTime": closing_time,
            "imageUrl": image_url,
            "url": lot_url,
            "auctionName": catalog_info.get("name",""),
            "sourceId": source_id,
            "sourceName": catalog_info.get("companyName", catalog_info.get("name","")),
            "sourceColor": catalog_info.get("color","#64748b"),
            "location": catalog_info.get("location","Fresno Area, CA"),
            "pickup": catalog_info.get("pickup","Check listing"),
            "miles": catalog_info.get("miles"),
            "category": "",
            "tags": [],
            "isSteal": False,
            "scrapedAt": datetime.now(timezone.utc).isoformat(),
        })

    return lots


# ── COMPANY CATALOG LOOKUP ────────────────────────────────────────────────────

def get_open_catalogs_for_company(company):
    """Get open catalogs for a known company ID."""
    endpoints = [
        f"{API_BASE}/api/v1/catalogs?companyId={company['id']}&status=open",
        f"{API_BASE}/api/v1/company/{company['id']}/catalogs?status=open",
        f"{API_BASE}/api/v1/auctions?companyId={company['id']}&status=open",
    ]
    for url in endpoints:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                catalogs = parse_catalog_response(data)
                if catalogs:
                    # Enrich with known company info
                    for c in catalogs:
                        c.update({
                            "companyName": company["name"],
                            "location": company["location"],
                            "pickup": company["pickup"],
                            "color": company["color"],
                            "miles": company["miles"],
                        })
                    return catalogs
        except: pass
    return []


# ── AI ENRICHMENT ─────────────────────────────────────────────────────────────

def enrich_lots_with_ai(lots):
    if not ANTHROPIC_API_KEY or not lots:
        for lot in lots:
            lot["normalizedTitle"] = lot["rawTitle"]
            lot["category"] = "Other"
        return lots

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        print("anthropic not installed")
        return lots

    BATCH = 30
    print(f"\nEnriching {len(lots)} lots with Claude AI...")

    for i in range(0, len(lots), BATCH):
        batch = lots[i:i+BATCH]
        titles = [{"index": j, "title": lot["rawTitle"]} for j, lot in enumerate(batch)]
        prompt = f"""Organize these auction lot titles for a family in Fresno, CA.
For each, return a JSON array with:
- index: same number
- normalizedTitle: clean readable name (e.g. "Nail Gun" not "BOSTITCH 15GA FINISH NAILER")
- category: one of {json.dumps(CATEGORY_LIST)}
- tags: 6-12 search terms someone might use (synonyms, brand, type, use case)
- isSteal: true if common useful item that sounds like a good deal

Input: {json.dumps(titles)}
Return ONLY valid JSON array, no markdown, no explanation."""

        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role":"user","content":prompt}]
            )
            raw = r.content[0].text.strip().replace("```json","").replace("```","").strip()
            results = json.loads(raw)
            for res in results:
                idx = res.get("index",0)
                if idx < len(batch):
                    batch[idx]["normalizedTitle"] = res.get("normalizedTitle", batch[idx]["rawTitle"])
                    batch[idx]["category"] = res.get("category","Other")
                    batch[idx]["tags"] = res.get("tags",[])
                    batch[idx]["isSteal"] = res.get("isSteal",False)
        except Exception as e:
            print(f"  AI batch error: {e}")
            for lot in batch:
                lot["normalizedTitle"] = lot["rawTitle"]
                lot["category"] = "Other"

        print(f"  Enriched {min(i+BATCH,len(lots))}/{len(lots)}")
        time.sleep(0.5)

    return lots


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
    seen_catalog_ids = set()

    # Step 1: Zip radius discovery
    discovered = discover_catalogs()
    for cat in discovered:
        cid = cat["catalogId"]
        if cid in seen_catalog_ids: continue
        seen_catalog_ids.add(cid)
        lots = get_lots_for_catalog(cid, cat)
        if lots:
            all_lots.extend(lots)
            all_sources.append({
                "id": cat.get("companyId") or re.sub(r'[^a-z0-9]','_',cat["companyName"].lower())[:20],
                "name": cat["companyName"],
                "color": cat["color"],
                "location": cat["location"],
                "pickup": "Check listing",
                "miles": cat["miles"],
                "url": cat["url"],
            })
        time.sleep(0.5)

    # Step 2: Known companies
    for company in KNOWN_COMPANY_IDS:
        catalogs = get_open_catalogs_for_company(company)
        if not catalogs:
            print(f"  — {company['name']}: no open catalogs found via API")
            continue
        for cat in catalogs:
            cid = cat["catalogId"]
            if cid in seen_catalog_ids: continue
            seen_catalog_ids.add(cid)
            lots = get_lots_for_catalog(cid, cat)
            if lots:
                all_lots.extend(lots)
                if not any(s["id"]==company["id"] for s in all_sources):
                    all_sources.append({
                        "id": company["id"],
                        "name": company["name"],
                        "color": company["color"],
                        "location": company["location"],
                        "pickup": company["pickup"],
                        "miles": company["miles"],
                        "url": f"https://hibid.com/company/{company['id']}",
                    })
            time.sleep(0.5)

    print(f"\nTotal lots: {len(all_lots)}")

    # Deduplicate
    seen = set()
    unique = []
    for lot in all_lots:
        key = f"{lot['sourceName']}_{lot['rawTitle'][:40]}"
        if key not in seen:
            seen.add(key)
            unique.append(lot)

    unique = enrich_lots_with_ai(unique)
    unique = flag_steals(unique)

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
        "totalLots": len(unique),
        "sources": unique_sources,
        "lots": unique,
    }

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Saved {len(unique)} lots to {DATA_JSON_PATH}")
    print("Done!")


if __name__ == "__main__":
    main()
