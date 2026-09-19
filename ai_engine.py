import os
import requests
from openai import OpenAI
from processing.contracts import BriefingPayload

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def fetch_holding_news(tickers: list[str]) -> str:
    """
    Fetches news headlines related to the client's core holdings.
    Replace with your actual News API endpoint and API key.
    """
    if not tickers:
        return "No specific ticker news available."
    
    query = " OR ".join(tickers[:3])  # Top 3 positions to avoid noisy searches
    news_api_url = f"https://newsapi.org/v2/everything?q={query}&sortBy=publishedAt&pageSize=3&apiKey={os.getenv('NEWS_API_KEY')}"
    
    try:
        response = requests.get(news_api_url, timeout=5)
        if response.status_code == 200:
            articles = response.json().get("articles", [])
            headlines = [f"- {a['title']}: {a['description']}" for a in articles[:3]]
            return "\n".join(headlines) if headlines else "No recent market news found."
    except Exception as e:
        return f"News retrieval unavailable: {str(e)}"
    
    return "No recent market news found."


def generate_wealth_briefing(payload: BriefingPayload) -> str:
    """
    Takes pre-calculated BriefingPayload and generates a concise summary (< 300 words).
    """
    # Extract key tickers/holdings from payload for targeted news search
    top_tickers = [pos.security_id for pos in getattr(payload, "top_positions", []) if hasattr(pos, "security_id")]
    news_summary = fetch_holding_news(top_tickers)

    # System prompt enforcing role and word limit
    system_prompt = (
        "You are an elite wealth management AI assistant. "
        "Analyze the pre-calculated portfolio metrics, rule findings, and news. "
        "Deliver a highly structured, concise briefing for the advisor. "
        "STRICT LIMIT: The entire response MUST be under 250 words total."
    )

    # User prompt containing payload data and news
    user_prompt = f"""
    Client Portfolio Data:
    {payload.model_dump_json() if hasattr(payload, 'model_dump_json') else str(payload)}

    Relevant Live News:
    {news_summary}

    Generate the briefing using these 4 brief bulleted sections:
    1. **Portfolio Health & Cash Status**
    2. **Flagged Risks & Violations**
    3. **News Precautions & Market Opportunities**
    4. **Recommended Advisor Next Actions**
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.2,  # Low temperature for deterministic, factual outputs
        max_tokens=450    # Hard limit safety stop
    )

    return response.choices[0].message.content