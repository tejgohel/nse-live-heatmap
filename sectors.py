# ─────────────────────────────────────────────────────────────────────────────
#  sectors.py  —  symbol -> one sector label, for the heatmap
#
#  Built from the user's own FnO_Index_Wise.txt (208 F&O stocks, index-wise),
#  kept verbatim below so the mapping is auditable against that file.  Static
#  on purpose: fno_sectors.py downloads index constituents from NSE, and a
#  dashboard column is not worth a network dependency that can fail at 09:15.
#
#  ── Why a priority order is needed ──────────────────────────────────────────
#  Most stocks sit in SEVERAL indices — AXISBANK is in NIFTY 50, BANK, PRIVATE
#  BANK and FINANCIAL SERVICES all at once.  The column has room for one label,
#  so PRIORITY runs from most specific to most general and the first hit wins.
#
#  A few deliberate placements inside that order:
#    · PSU BANK / PRIVATE BANK beat BANK      -> SBIN "PSU Bank", HDFCBANK "Private Bank"
#    · AUTO and IT beat DEFENCE               -> BHARATFORG stays "Auto", KAYNES "IT",
#                                                while BEL / HAL / BDL / MAZDOCK /
#                                                COCHINSHIP / SOLARINDS read "Defence"
#    · DEFENCE beats CHEMICALS                -> SOLARINDS is defence, not a chemical
#    · OIL & GAS beats ENERGY                 -> RELIANCE / ONGC "Oil & Gas",
#                                                NTPC / NHPC "Energy"
#    · CEMENT beats INFRA and NIFTY 50        -> GRASIM / ULTRACEMCO "Cement"
#    · CONSUMER DURABLES beats CONSUMPTION    -> TITAN "Cons Durables"
#    · NIFTY 50 and MIDCAP SELECT are LAST — they are size buckets, not sectors,
#      and only apply to a stock that no sector index claims.
#
#  Known rough edges, inherited from the source list rather than introduced
#  here: BHARTIARTL and INDIGO land on "Infra" because no telecom or aviation
#  index in the file contains them, and LICI lands on "Midcap" because MIDCAP
#  SELECT is the only list it appears in.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

#  Verbatim from FnO_Index_Wise.txt — index name -> its constituents.
INDEX_MEMBERS: "dict[str, str]" = {
    "NIFTY 50": "ADANIENT, ADANIPORTS, APOLLOHOSP, ASIANPAINT, AXISBANK, BAJAJ-AUTO, BAJFINANCE, BAJAJFINSV, BEL, BHARTIARTL, CIPLA, COALINDIA, DRREDDY, EICHERMOT, ETERNAL, GRASIM, HCLTECH, HDFCBANK, HDFCLIFE, HINDALCO, HINDUNILVR, ICICIBANK, ITC, INFY, INDIGO, JSWSTEEL, JIOFIN, KOTAKBANK, LT, M&M, MARUTI, MAXHEALTH, NTPC, NESTLEIND, ONGC, POWERGRID, RELIANCE, SBILIFE, SHRIRAMFIN, SBIN, SUNPHARMA, TCS, TATACONSUM, TMPV, TATASTEEL, TECHM, TITAN, TRENT, ULTRACEMCO, WIPRO",
    "NIFTY BANK": "AUBANK, AXISBANK, BANKBARODA, CANBK, FEDERALBNK, HDFCBANK, ICICIBANK, IDFCFIRSTB, INDUSINDBK, KOTAKBANK, PNB, SBIN, UNIONBANK, YESBANK",
    "NIFTY PRIVATE BANK": "AXISBANK, BANDHANBNK, FEDERALBNK, HDFCBANK, ICICIBANK, IDFCFIRSTB, INDUSINDBK, KOTAKBANK, RBLBANK, YESBANK",
    "NIFTY PSU BANK": "BANKBARODA, BANKINDIA, CANBK, INDIANB, PNB, SBIN, UNIONBANK",
    "NIFTY FINANCIAL SERVICES": "PNBHOUSING, NAM-INDIA, MOTILALOFS, MCX, MANAPPURAM, LTF, IREDA, IRFC, IEX, ICICIPRULI, AXISBANK, BSE, BAJFINANCE, BAJAJFINSV, CHOLAFIN, HDFCBANK, HDFCLIFE, ICICIBANK, ICICIGI, JIOFIN, KOTAKBANK, LICHSGFIN, MFSL, MUTHOOTFIN, PFC, RECLTD, SBICARD, SBILIFE, SHRIRAMFIN, SBIN, 360ONE, ABCAPITAL, ANGELONE, BAJAJHLDNG, CAMS, CDSL, KFINTECH, HDFCAMC",
    "NIFTY IT": "KAYNES, COFORGE, HCLTECH, INFY, LTM, MPHASIS, OFSS, PERSISTENT, TCS, TECHM, WIPRO",
    "NIFTY MIDSMALL IT & TELECOM": "KAYNES, COFORGE, INDUSTOWER, KPITTECH, MPHASIS, OFSS, PERSISTENT, TATAELXSI, IDEA",
    "NIFTY AUTO": "HYUNDAI, ASHOKLEY, BAJAJ-AUTO, BHARATFORG, BOSCHLTD, EICHERMOT, HEROMOTOCO, M&M, MARUTI, MOTHERSON, SONACOMS, TVSMOTOR, TMPV, TIINDIA, UNOMINDA, FORCEMOT",
    "NIFTY PHARMA": "ALKEM, AUROPHARMA, BIOCON, CIPLA, DIVISLAB, DRREDDY, GLENMARK, LAURUSLABS, LUPIN, MANKIND, SUNPHARMA, TORNTPHARM, ZYDUSLIFE",
    "NIFTY HEALTHCARE": "ALKEM, APOLLOHOSP, AUROPHARMA, BIOCON, CIPLA, DIVISLAB, DRREDDY, FORTIS, GLENMARK, LAURUSLABS, LUPIN, MANKIND, MAXHEALTH, SUNPHARMA, TORNTPHARM, ZYDUSLIFE",
    "NIFTY FMCG": "BRITANNIA, COLPAL, DABUR, GODREJCP, HINDUNILVR, ITC, MARICO, NESTLEIND, PATANJALI, RADICO, TATACONSUM, UNITDSPR, VBL, GODFRYPHLP",
    "NIFTY METAL": "APLAPOLLO, ADANIENT, HINDALCO, HINDZINC, JSWSTEEL, JINDALSTEL, NMDC, NATIONALUM, SAIL, TATASTEEL, VEDL",
    "NIFTY ENERGY": "WAAREEENER, PREMIERENE, ABB, ADANIENSOL, ADANIGREEN, ADANIPOWER, BHEL, BPCL, CGPOWER, COALINDIA, GAIL, GVT&D, HINDPETRO, POWERINDIA, IOC, INOXWIND, JSWENERGY, NHPC, NTPC, ONGC, OIL, PETRONET, POWERGRID, RELIANCE, SIEMENS, SUZLON, TATAPOWER",
    "NIFTY OIL & GAS": "BPCL, GAIL, HINDPETRO, IOC, ONGC, OIL, PETRONET, RELIANCE",
    "NIFTY REALTY": "DLF, GODREJPROP, LODHA, OBEROIRLTY, PHOENIXLTD, PRESTIGE",
    "NIFTY INFRASTRUCTURE": "RVNL, NBCC, KEI, ADANIGREEN, ADANIPORTS, AMBUJACEM, APOLLOHOSP, ASHOKLEY, BHARATFORG, BPCL, BHARTIARTL, CGPOWER, CUMMINSIND, DLF, FORTIS, GAIL, GRASIM, HINDPETRO, INDHOTEL, IOC, INDUSTOWER, INDIGO, LT, MAXHEALTH, NTPC, ONGC, POWERGRID, RELIANCE, MOTHERSON, SHREECEM, SUZLON, TATAPOWER, ULTRACEMCO, CONCOR, DELHIVERY, GMRAIRPORT",
    "NIFTY CONSUMPTION": "VMM, PAGEIND, NYKAA, JUBLFOOD, ADANIPOWER, APOLLOHOSP, ASIANPAINT, DMART, BAJAJ-AUTO, BHARTIARTL, BRITANNIA, DLF, DIXON, EICHERMOT, ETERNAL, GODREJCP, HAVELLS, HEROMOTOCO, HINDUNILVR, ITC, INDHOTEL, NAUKRI, INDIGO, M&M, MARUTI, MAXHEALTH, NESTLEIND, TVSMOTOR, TATACONSUM, TATAPOWER, TITAN, TRENT, UNITDSPR, VBL",
    "NIFTY CONSUMER DURABLES": "AMBER, BLUESTARCO, CROMPTON, DIXON, HAVELLS, KALYANKJIL, PGEL, TITAN, VOLTAS, ASTRAL, SUPREMEIND",
    "NIFTY CEMENT": "ULTRACEMCO, GRASIM, AMBUJACEM, SHREECEM, DALBHARAT",
    "NIFTY CHEMICALS": "PIIND, PIDILITIND, SRF, SOLARINDS, UPL",
    "NIFTY INDIA DEFENCE": "BDL, BEL, BHARATFORG, COCHINSHIP, HAL, MAZDOCK, SOLARINDS, KAYNES",
    "NIFTY MIDCAP SELECT": "AUBANK, ASHOKLEY, AUROPHARMA, BSE, BHARATFORG, BHEL, DIXON, FORTIS, HEROMOTOCO, HINDPETRO, INDIANB, INDUSTOWER, INDUSINDBK, NAUKRI, LICI, LUPIN, MARICO, PAYTM, POLICYBZR, PERSISTENT, POLYCAB, SRF, SUZLON, SWIGGY, YESBANK",
}

#  (index name, the short label the column shows) — FIRST MATCH WINS.
PRIORITY: "list[tuple[str, str]]" = [
    ("NIFTY PSU BANK",             "PSU Bank"),
    ("NIFTY PRIVATE BANK",         "Private Bank"),
    ("NIFTY BANK",                 "Bank"),
    ("NIFTY IT",                   "IT"),
    ("NIFTY MIDSMALL IT & TELECOM", "IT / Telecom"),
    ("NIFTY AUTO",                 "Auto"),
    ("NIFTY PHARMA",               "Pharma"),
    ("NIFTY HEALTHCARE",           "Healthcare"),
    ("NIFTY INDIA DEFENCE",        "Defence"),
    ("NIFTY FMCG",                 "FMCG"),
    ("NIFTY METAL",                "Metal"),
    ("NIFTY OIL & GAS",            "Oil & Gas"),
    ("NIFTY ENERGY",               "Energy"),
    ("NIFTY REALTY",               "Realty"),
    ("NIFTY CEMENT",               "Cement"),
    ("NIFTY CHEMICALS",            "Chemicals"),
    ("NIFTY CONSUMER DURABLES",    "Cons Durables"),
    ("NIFTY FINANCIAL SERVICES",   "Financials"),
    ("NIFTY INFRASTRUCTURE",       "Infra"),
    ("NIFTY CONSUMPTION",          "Consumption"),
    ("NIFTY 50",                   "Nifty 50"),
    ("NIFTY MIDCAP SELECT",        "Midcap"),
]

UNKNOWN = "—"


def _build() -> "dict[str, str]":
    members = {name: {s.strip().upper() for s in csv.split(",") if s.strip()}
               for name, csv in INDEX_MEMBERS.items()}
    out: "dict[str, str]" = {}
    for index_name, label in PRIORITY:
        for sym in members.get(index_name, ()):
            out.setdefault(sym, label)       # first (highest priority) wins
    return out


SECTOR: "dict[str, str]" = _build()

#  Every index a symbol appears in — handy when a placement looks wrong.
ALL_INDICES: "dict[str, list[str]]" = {}
for _name, _csv in INDEX_MEMBERS.items():
    for _s in _csv.split(","):
        _s = _s.strip().upper()
        if _s:
            ALL_INDICES.setdefault(_s, []).append(_name)


def of(symbol: str) -> str:
    """The one sector label for `symbol`, or an em dash when nothing claims it."""
    return SECTOR.get((symbol or "").strip().upper(), UNKNOWN)


def indices_of(symbol: str) -> "list[str]":
    """Every index the symbol belongs to — for checking a surprising label."""
    return ALL_INDICES.get((symbol or "").strip().upper(), [])


def coverage(symbols) -> "tuple[list[str], dict[str, int]]":
    """(symbols with no sector, label -> count) for a universe."""
    missing = [s for s in symbols if of(s) == UNKNOWN]
    counts: "dict[str, int]" = {}
    for s in symbols:
        lbl = of(s)
        counts[lbl] = counts.get(lbl, 0) + 1
    return missing, dict(sorted(counts.items(), key=lambda x: -x[1]))


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"  {len(SECTOR)} symbols mapped across {len(INDEX_MEMBERS)} indices\n")
    by: "dict[str, list[str]]" = {}
    for sym, lbl in SECTOR.items():
        by.setdefault(lbl, []).append(sym)
    for _idx, lbl in PRIORITY:
        if lbl in by:
            syms = sorted(by.pop(lbl))
            print(f"  {lbl:<14} {len(syms):>3}  {', '.join(syms)}\n")
