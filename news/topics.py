"""Public vocabulary, never client-specific rules or canned articles.

Each market match requires BOTH the exposure subject and a financial event.
Unmapped categories remain visible as skipped; do not guess a sector from a name.
"""
EVENTS = (
    "earnings", "revenue", "profits", "profit warning", "dividend", "acquisition",
    "merger", "default", "bankruptcy", "credit downgrade", "sanctions", "tariffs",
    "regulation", "regulatory", "antitrust", "interest rate", "interest rates",
    "rate cut", "rate cuts", "rate hike", "rate hikes", "monetary policy",
    "inflation", "recession", "economic growth", "bond yields", "selloff",
    "sell-off", "volatility", "supply disruption", "supply disruptions",
    "exports", "exchange rate", "exchange rates", "appreciation", "depreciation",
    "rallies", "plunges", "rally", "devaluation", "intervention", "outlook",
)

SECTORS = {
    "Financials": ("financial sector", "financial services sector", "financial stocks"),
    "Information Technology": ("technology sector", "tech sector"),
    "Health Care": ("healthcare sector", "health care sector"),
    "Industrials": ("industrial sector", "industrial stocks"),
    "Consumer Staples": ("consumer staples", "consumer staples sector"),
    "Consumer Discretionary": ("consumer discretionary", "consumer discretionary sector"),
    "Energy": ("energy sector", "energy companies"),
    "Materials": ("materials sector", "basic materials sector"),
    "Utilities": ("utilities sector", "utility stocks"),
    "Telecommunication Services": ("telecom sector", "telecommunications sector"),
    "Real Estate": ("real estate market", "property market", "REITs"),
}
REGIONS = {
    "Switzerland": ("Swiss economy", "Switzerland economy"),
    "North America": ("North American economy", "North America"),
    "Rest of Europe": ("European economy", "Europe economy"),
    "Japan": ("Japanese economy", "Japan economy"),
    "Asia/Pacific (ex Japan)": ("Asia Pacific economy", "Asia-Pacific economy"),
    "Great Britain": ("UK economy", "British economy"),
}
ASSETS = {
    "Shares": ("equity markets", "stock market"),
    "Bonds": ("bond market", "bond yields", "credit market"),
    "Real estate": ("real estate market", "property market"),
}
CURRENCY_GROUPS = {"Swiss francs": "CHF", "Euro": "EUR", "US-Dollar": "USD"}
# Names of institutions are public routing vocabulary, not inferred client attributes.
MONETARY = {
    "CHF": ("Swiss National Bank", "SNB", "Swiss franc"),
    "EUR": ("European Central Bank", "ECB", "euro"),
    "USD": ("Federal Reserve", "US dollar"),
    "GBP": ("Bank of England", "British pound"),
    "JPY": ("Bank of Japan", "Japanese yen"),
    "AUD": ("Reserve Bank of Australia", "Australian dollar"),
    "CAD": ("Bank of Canada", "Canadian dollar"),
    "NZD": ("Reserve Bank of New Zealand", "New Zealand dollar"),
    "SEK": ("Riksbank", "Swedish krona"),
    "NOK": ("Norges Bank", "Norwegian krone"),
    "CNY": ("People's Bank of China", "Chinese yuan"),
}
POLICY_EVENTS = (
    "interest rate", "interest rates", "rate cut", "rate cuts", "rate hike",
    "rate hikes", "monetary policy", "inflation", "exchange rate", "exchange rates",
    "intervention", "devaluation", "appreciation", "depreciation",
)
UNKNOWN = {"Unclassified", "Not classified", "Others", "Andere", "UNKNOWN"}
