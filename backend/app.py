from flask import Flask, request, jsonify
from flask_cors import CORS
from groq import Groq
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import base64
import json
import os
import re

load_dotenv()

app = Flask(__name__)
CORS(app)

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
TEXT_MODEL = "llama-3.3-70b-versatile"


def extract_json(text):
    text = text.strip()
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    return json.loads(text[start:end])


#Scraper
def scrape_landing_page(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=10)
    raw_html = response.text
    soup = BeautifulSoup(raw_html, "html.parser")

   
    soup_for_inject = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()

    sections = {
        "title": soup.title.string.strip() if soup.title else "",
        "h1": [h.get_text(strip=True) for h in soup.find_all("h1")][:3],
        "h2": [h.get_text(strip=True) for h in soup.find_all("h2")][:5],
        "hero_text": "",
        "cta_buttons": [b.get_text(strip=True) for b in soup.find_all(
            ["button", "a"],
            class_=lambda c: c and any(x in c.lower() for x in ["cta", "btn", "button", "primary"])
        )][:5],
        "body_snippets": [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40][:3],
        "raw_soup": soup_for_inject
    }

    hero_candidates = soup.find_all(
        ["section", "div"],
        class_=lambda c: c and any(x in c.lower() for x in ["hero", "banner", "header", "intro"])
    )
    if hero_candidates:
        sections["hero_text"] = hero_candidates[0].get_text(separator=" ", strip=True)[:300]

    return sections


# AD Analyze 
def analyze_ad(image_bytes, mime_type):
    prompt = """Analyze this ad image. Reply with ONLY this JSON, no other text:
{"headline":"main message","offer":"offer or value prop","cta":"call to action","tone":"tone of ad","target_audience":"who this targets","emotion":"primary emotion","keywords":["word1","word2"]}"""

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_bytes}"}},
                {"type": "text", "text": prompt}
            ]
        }],
        max_tokens=400
    )

    text = response.choices[0].message.content
    print("AD RAW:", text[:200])
    return extract_json(text)


# Personalize Page
def personalize_page(page_sections, ad_analysis):
    prompt = f"""You are a CRO expert. Rewrite this landing page to match the ad.

AD: {json.dumps(ad_analysis)}

PAGE:
Title: {page_sections['title']}
H1: {page_sections['h1']}
H2: {page_sections['h2']}
CTA: {page_sections['cta_buttons']}

Reply with ONLY this JSON, no explanation, no markdown:
{{"personalized":{{"title":"new title","h1":"new headline","h2":"new subheadline","cta":"new cta text","hero_subtext":"1-2 sentences"}},"changes":[{{"element":"H1","original":"old text","updated":"new text","reason":"why"}}],"cro_score_before":35,"cro_score_after":75,"summary":"2 sentence strategy explanation"}}"""

    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000
    )

    text = response.choices[0].message.content
    print("PERSONALIZE RAW:", text[:300])
    return extract_json(text)


# Personalized HTML
def build_personalized_html(soup, personalized, landing_url):
    """Inject personalized copy into the real scraped page HTML."""
    try:
        if soup.title and personalized.get("title"):
            soup.title.string = personalized["title"]

        h1_tags = soup.find_all("h1")
        if h1_tags and personalized.get("h1"):
            h1_tags[0].string = personalized["h1"]

        h2_tags = soup.find_all("h2")
        if h2_tags and personalized.get("h2"):
            h2_tags[0].string = personalized["h2"]

        cta_tags = soup.find_all(
            ["button", "a"],
            class_=lambda c: c and any(x in c.lower() for x in ["cta", "btn", "button", "primary"])
        )
        if cta_tags and personalized.get("cta"):
            cta_tags[0].string = personalized["cta"]

        for tag in soup.find_all(["a", "link"], href=True):
            if tag["href"].startswith("/"):
                tag["href"] = landing_url.rstrip("/") + tag["href"]
        for tag in soup.find_all(["img", "script"], src=True):
            if tag["src"].startswith("/"):
                tag["src"] = landing_url.rstrip("/") + tag["src"]

        banner = soup.new_tag("div")
        banner["style"] = "position:fixed;top:0;left:0;right:0;background:#4f46e5;color:white;text-align:center;padding:8px;font-size:13px;font-family:sans-serif;z-index:99999;"
        banner.string = "✦ Troopod AI — Personalized landing page preview"
        if soup.body:
            soup.body.insert(0, banner)

        return str(soup)
    except Exception as e:
        print("HTML inject error:", e)
        return None


# Flask 
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/personalize", methods=["POST"])
def personalize():
    try:
        landing_url = request.form.get("url", "").strip()
        ad_file = request.files.get("ad_image")

        if not landing_url or not ad_file:
            return jsonify({"error": "Both a landing page URL and ad image are required."}), 400

        image_bytes = base64.b64encode(ad_file.read()).decode("utf-8")
        mime_type = ad_file.content_type or "image/jpeg"

        page_sections = scrape_landing_page(landing_url)
        ad_analysis = analyze_ad(image_bytes, mime_type)
        result = personalize_page(page_sections, ad_analysis)

        # Build HTML page
        personalized_html = build_personalized_html(
            page_sections["raw_soup"],
            result["personalized"],
            landing_url
        )

        return jsonify({
            "success": True,
            "landing_url": landing_url,
            "ad_analysis": ad_analysis,
            "personalization": result,
            "personalized_html": personalized_html,
            "original_sections": {
                "title": page_sections["title"],
                "h1": page_sections["h1"],
                "h2": page_sections["h2"],
                "cta_buttons": page_sections["cta_buttons"]
            }
        })

    except requests.exceptions.RequestException:
        return jsonify({"error": "Could not fetch the landing page. Check the URL and try again."}), 400
    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI response parsing failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)