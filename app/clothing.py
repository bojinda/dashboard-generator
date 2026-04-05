# clothing.py
def choose_clothing_profile(temp_c: float, precip: str, intensity: str, icy: bool) -> dict:
    profile = {
        "legs": "long work pants",
        "boots": "black safety boots",
        "vest": "orange high-visibility safety vest",
        "outerwear": "",
        "base_top": "",
        "extras": [],
    }

    precip = (precip or "").lower()
    intensity = (intensity or "").lower()

    frozen = precip in {"snow", "sleet", "freezing_rain", "mixed"}
    heavy_rain = precip == "rain" and intensity in {"heavy", "downpour", "storm", "thunderstorm"}

    if temp_c >= 30:
        profile["base_top"] = "black or blue breathable dry-fit t-shirt"
    elif temp_c >= 19:
        profile["base_top"] = "black or blue breathable dry-fit t-shirt"
        if heavy_rain:
            profile["outerwear"] = "black waterproof rainsuit with black overalls and a long hooded black coat"
    elif temp_c >= 11:
        profile["base_top"] = "dark navy blue hooded sweater"
        if heavy_rain:
            profile["outerwear"] = "black waterproof rainsuit with black overalls and a long hooded black coat"
    elif temp_c >= 1:
        profile["base_top"] = "none visible under outerwear"
        if precip == "rain":
            profile["outerwear"] = "black waterproof rainsuit with black overalls and a long hooded black coat"
        else:
            profile["outerwear"] = "black nylon weatherproof spring jacket"
    else:
        profile["outerwear"] = "heavy winter work outerwear"

    if frozen and not profile["outerwear"]:
        profile["outerwear"] = "heavy winter work outerwear"

    if icy or frozen:
        profile["extras"].append("ice cleats fitted over the boots")

    return profile