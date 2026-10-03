import requests

def get_geo_location(ip_address):
    try:
        response = requests.get(f"http://ip-api.com/json/{ip_address}")
        data = response.json()

        return {
            "country": data.get("country", "Unknown"),
            "city": data.get("city", "Unknown")
        }

    except:
        return {
            "country": "Unknown",
            "city": "Unknown"
        }