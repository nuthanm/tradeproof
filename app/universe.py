"""Nifty 50 + representative India Top 100 NSE symbols (Yahoo .NS suffix applied at fetch)."""

# Official-ish Nifty 50 set (update periodically)
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "ITC", "SBIN",
    "LT", "AXISBANK", "BAJFINANCE", "KOTAKBANK", "HINDUNILVR", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "WIPRO", "ULTRACEMCO", "NESTLEIND", "POWERGRID", "NTPC",
    "M&M", "HCLTECH", "TATAMOTORS", "ADANIENT", "ADANIPORTS", "TECHM", "ONGC", "COALINDIA",
    "JSWSTEEL", "TATASTEEL", "BAJAJFINSV", "GRASIM", "CIPLA", "DRREDDY", "APOLLOHOSP",
    "EICHERMOT", "HEROMOTOCO", "BPCL", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "DIVISLAB",
    "BRITANNIA", "TATACONSUM", "BAJAJ-AUTO", "HINDALCO", "BEL", "TRENT",
]

# Extra large/mid names to approximate "Top 100" coverage beyond Nifty 50
TOP_100_EXTRA = [
    "DMART", "ZOMATO", "IRCTC", "HAL", "POLYCAB", "SIEMENS", "ABB", "PIDILITIND",
    "GODREJCP", "DABUR", "HAVELLS", "AMBUJACEM", "SHREECEM", "DLF", "LODHA",
    "PNB", "BANKBARODA", "CANBK", "UNIONBANK", "IOC", "GAIL", "VEDL", "HINDPETRO",
    "INDIGO", "NAUKRI", "PERSISTENT", "LTIM", "COFORGE", "MPHASIS", "PAGEIND",
    "DIXON", "TVSMOTOR", "BOSCHLTD", "MRF", "CHOLAFIN", "MUTHOOTFIN", "PFC", "RECLTD",
    "IRFC", "NHPC", "SJVN", "AMBER", "SOLARINDS", "CUMMINSIND", "CGPOWER", "TIINDIA",
    "MAXHEALTH", "MANKIND", "TORNTPHARM", "LUPIN", "AUROPHARMA", "BIOCON",
]

ALL_EQUITIES = sorted(set(NIFTY_50 + TOP_100_EXTRA))

# Penny screen candidates — refreshed for names that often trade near/below ₹25.
# Live price filter is applied in scan_pennies(); keep this list broad.
PENNY_CANDIDATES = [
    "IDEA",
    "YESBANK",
    "JPPOWER",
    "RTNPOWER",
    "SOUTHBANK",
    "DISHTV",
    "ALOKINDS",
    "RPOWER",
    "SUZLON",
    "GTLINFRA",
    "PCJEWELLER",
    "JPASSOCIAT",
    "SPICEJET",
    "EASEMYTRIP",
    "SEPC",
    "RTNINDIA",
    "NETWORK18",
    "TV18BRDCST",
    "UCOBANK",
    "CENTRALBK",
    "IOB",
    "IDBI",
    "PNB",
    "SAIL",
    "NATIONALUM",
    "HINDCOPPER",
    "J&KBANK",
    "HFCL",
    "TTML",
    "MTNL",
]

INDEX_YF = {
    "NIFTY": "^NSEI",
    "SENSEX": "^BSESN",
    "VIX": "^INDIAVIX",
}
