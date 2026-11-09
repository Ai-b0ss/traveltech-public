"""Reviewed Russian names -> canonical search city and ISO country.
This is a *bounded* lexicon, not a universal gazetteer or a claim of availability.
Any provider resolution must still pass exact City+country validation.
"""

import re

ENTRIES = {
    "Paris|FR": "париж|париже",
    "Frankfurt am Main|DE": "франкфурт-на-майне|франкфурте-на-майне",
    "Rome|IT": "рим|риме",
    "Prague|CZ": "прага|праге|прагу",
    "Berlin|DE": "берлин|берлине",
    "Munich|DE": "мюнхен|мюнхене",
    "Vienna|AT": "вена|вене|вену",
    "Budapest|HU": "будапешт|будапеште",
    "Warsaw|PL": "варшава|варшаве|варшаву",
    "Krakow|PL": "краков|кракове",
    "Amsterdam|NL": "амстердам|амстердаме",
    "Brussels|BE": "брюссель|брюсселе",
    "London|GB": "лондон|лондоне",
    "Edinburgh|GB": "эдинбург|эдинбурге",
    "Barcelona|ES": "барселона|барселоне|барселону",
    "Madrid|ES": "мадрид|мадриде",
    "Lisbon|PT": "лиссабон|лиссабоне",
    "Athens|GR": "афины|афинах",
    "Nice|FR": "ницца|ницце|ниццу",
    "Milan|IT": "милан|милане",
    "Venice|IT": "венеция|венеции|венецию",
    "Florence|IT": "флоренция|флоренции|флоренцию",
    "Tokyo|JP": "токио",
    "Kyoto|JP": "киото",
    "Osaka|JP": "осака|осаке|осаку",
    "Seoul|KR": "сеул|сеуле",
    "Bangkok|TH": "бангкок|бангкоке",
    "Phuket|TH": "пхукет|пхукете",
    "Singapore|SG": "сингапур|сингапуре",
    "Antalya|TR": "анталья|анталье|анталью",
    "Bodrum|TR": "бодрум|бодруме",
    "Ankara|TR": "анкара|анкаре|анкару",
    "Cairo|EG": "каир|каире",
    "Dubai|AE": "дубай|дубае",
    "Istanbul|TR": "стамбул|стамбуле",
    "Sochi|RU": "сочи",
    "Moscow|RU": "москва|москве|москву",
    "Kazan|RU": "казань|казани",
    "Vladivostok|RU": "владивосток|владивостоке",
    "Saint Petersburg|RU": "санкт-петербург|санкт-петербурге|петербург|петербурге|питер|питере",
    "New York|US": "нью-йорк|нью-йорке",
    "Los Angeles|US": "лос-анджелес|лос-анджелесе",
    "San Francisco|US": "сан-франциско",
    "Chicago|US": "чикаго",
    "Toronto|CA": "торонто",
    "Vancouver|CA": "ванкувер|ванкувере",
    "Tbilisi|GE": "тбилиси",
    "Yerevan|AM": "ереван|ереване",
    "Baku|AZ": "баку",
    "Almaty|KZ": "алматы",
    "Astana|KZ": "астана|астане|астану",
    "Murom|RU": "муром|муроме|мурому|мурома",
    "Suzdal|RU": "суздаль|суздале|суздали",
    "Vladimir|RU": "владимир|владимире",
    "Yaroslavl|RU": "ярославль|ярославле",
    "Kostroma|RU": "кострома|костроме|кострому",
    "Nizhny Novgorod|RU": "нижний новгород|нижнем новгороде|нижнего новгорода",
    "Samara|RU": "самара|самаре|самару",
    "Yekaterinburg|RU": "екатеринбург|екатеринбурге",
    "Kaliningrad|RU": "калининград|калининграде",
    "Magadan|RU": "магадан|магадане|магадану|магадана",
    "Syktyvkar|RU": "сыктывкар|сыктывкаре|сыктывкару|сыктывкара",
    "Novosibirsk|RU": "новосибирск|новосибирске",
    "Krasnoyarsk|RU": "красноярск|красноярске",
    "Tyumen|RU": "тюмень|тюмени",
    "Ufa|RU": "уфа|уфе|уфу",
    "Perm|RU": "пермь|перми",
    "Voronezh|RU": "воронеж|воронеже",
    "Volgograd|RU": "волгоград|волгограде",
    "Murmansk|RU": "мурманск|мурманске",
    "Irkutsk|RU": "иркутск|иркутске",
    "Khabarovsk|RU": "хабаровск|хабаровске",
    "Rostov-on-Don|RU": "ростов-на-дону|ростове-на-дону|ростов|ростове",
    "Krasnodar|RU": "краснодар|краснодаре",
    "Tomsk|RU": "томск|томске",
    "Omsk|RU": "омск|омске",
    "Chelyabinsk|RU": "челябинск|челябинске",
    "Saratov|RU": "саратов|саратове",
    "Tula|RU": "тула|туле|тулу",
    "Arkhangelsk|RU": "архангельск|архангельске",
    "Petrozavodsk|RU": "петрозаводск|петрозаводске",
    "Astrakhan|RU": "астрахань|астрахани",
    "Odintsovo|RU": "одинцово|одинцове|одинцова",
    "Yakutsk|RU": "якутск|якутске|якутску",
    "Pyatigorsk|RU": "пятигорск|пятигорске|пятигорску",
    "Kislovodsk|RU": "кисловодск|кисловодске",
    "Essentuki|RU": "ессентуки|ессентуках",
    "Anapa|RU": "анапа|анапе|анапу",
    "Gelendzhik|RU": "геленджик|геленджике",
    "Derbent|RU": "дербент|дербенте",
    "Norilsk|RU": "норильск|норильске",
    "Krasnogorsk|RU": "красногорск|красногорске",
    "Khimki|RU": "химки|химках",
    "Podolsk|RU": "подольск|подольске",
    "Mytishchi|RU": "мытищи|мытищах",
    "Lyubertsy|RU": "люберцы|люберцах",
    "Nalchik|RU": "нальчик|нальчике",
    "Stavropol|RU": "ставрополь|ставрополе",
    "Vladikavkaz|RU": "владикавказ|владикавказе",
    "Grozny|RU": "грозный|грозном",
    "Makhachkala|RU": "махачкала|махачкале|махачкалу",
    "Surgut|RU": "сургут|сургуте",
    "Chita|RU": "чита|чите|читу",
    "Ulan-Ude|RU": "улан-удэ",
    "Petropavlovsk-Kamchatsky|RU": "петропавловск-камчатский|петропавловске-камчатском",
    "Yuzhno-Sakhalinsk|RU": "южно-сахалинск|южно-сахалинске",
    "Tver|RU": "тверь|твери",
    "Ivanovo|RU": "иваново",
    "Ryazan|RU": "рязань|рязани",
    "Penza|RU": "пенза|пензе|пензу",
    "Lipetsk|RU": "липецк|липецке",
    "Tambov|RU": "тамбов|тамбове",
    "Smolensk|RU": "смоленск|смоленске",
    "Bryansk|RU": "брянск|брянске",
    "Orel|RU": "орёл|орел|орле",
    "Kursk|RU": "курск|курске",
    "Belgorod|RU": "белгород|белгороде",
    "Cheboksary|RU": "чебоксары|чебоксарах",
    "Saransk|RU": "саранск|саранске",
    "Yoshkar-Ola|RU": "йошкар-ола|йошкар-оле|йошкар-олу",
    "Izhevsk|RU": "ижевск|ижевске",
    "Orenburg|RU": "оренбург|оренбурге",
    "Kirov|RU": "киров|кирове",
    "Kurgan|RU": "курган|кургане",
    "Barnaul|RU": "барнаул|барнауле",
    "Kemerovo|RU": "кемерово",
    "Novokuznetsk|RU": "новокузнецк|новокузнецке",
    "Abakan|RU": "абакан|абакане",
    "Blagoveshchensk|RU": "благовещенск|благовещенске",
    "Veliky Novgorod|RU": "великий новгород|великом новгороде",
    "Pskov|RU": "псков|пскове",
    "Vyborg|RU": "выборг|выборге",
    "Torzhok|RU": "торжок|торжке",
    "Pereslavl-Zalessky|RU": "переславль-залесский|переславле-залесском|переславль",
    "Uglich|RU": "углич|угличе",
    "Rybinsk|RU": "рыбинск|рыбинске",
    "Balashikha|RU": "балашиха|балашихе|балашиху",
    "Korolev|RU": "королёв|королев|королёве|королеве",
    "Kolomna|RU": "коломна|коломне|коломну",
    "Domodedovo|RU": "домодедово",
    "Serpukhov|RU": "серпухов|серпухове",
    "Ramenskoye|RU": "раменское",
    "Dolgoprudny|RU": "долгопрудный|долгопрудном",
    "Pushkino|RU": "пушкино",
    "Reutov|RU": "реутов|реутове",
    "Sergiyev Posad|RU": "сергиев посад|сергиевом посаде|сергиева посада",
    "Dmitrov|RU": "дмитров|дмитрове",
    "Chekhov|RU": "чехов|чехове",
    "Zelenograd|RU": "зеленоград|зеленограде",
    "Tuapse|RU": "туапсе",
    "Novorossiysk|RU": "новороссийск|новороссийске",
    "Zheleznovodsk|RU": "железноводск|железноводске",
    "Mineralnye Vody|RU": "минеральные воды|минеральных водах|минводы",
    "Yalta|RU": "ялта|ялте|ялту",
    "Sevastopol|RU": "севастополь|севастополе",
}
ALIASES = {}
for key, forms in ENTRIES.items():
    city, country = key.split("|")
    for form in forms.split("|"):
        if form in ALIASES and ALIASES[form] != (city, country):
            raise RuntimeError("AMBIGUOUS_CURATED_NAME:" + form)
        ALIASES[form] = (city, country)
# Russian locative forms are only interpreted as city names inside an explicit
# travel/place sentence; never interpret unquoted hotel reviews as instructions.
PATTERN = re.compile(r"(?<!\w)(?:в|во|из|для\s+поездки\s+в)\s+([а-яё][а-яё-]{2,30})(?!\w)", re.I)


def matches(text):
    if not isinstance(text, str):
        return []
    # Scrub quoted supplier/review passages before interpreting cities.
    cleaned = re.sub(r'«[^»]*»|"[^"]*"', " ", text)
    found = []
    for match in PATTERN.finditer(cleaned):
        item = ALIASES.get(match.group(1).lower())
        if item:
            found.append(item)
    # An itinerary may begin with a city name without the preposition "в".
    # Only exact, curated names at sentence start are accepted here.
    for alias, item in sorted(ALIASES.items(), key=lambda entry: -len(entry[0])):
        boundary = r"(?=\s*(?:[,;:]|с\s+\d|на\s+\d|\d|$))"
        if re.match(r"^\s*" + re.escape(alias) + boundary, cleaned, re.I):
            found.append(item)
            break
    return found
