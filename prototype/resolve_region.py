"""Resolve a city within a requested country using returned geographic evidence."""
import math
def distance_km(a,b):
    lat1,lon1,lat2,lon2=map(math.radians,(a['latitude'],a['longitude'],b['latitude'],b['longitude']))
    dlat=lat2-lat1;dlon=lon2-lon1
    return 6371*2*math.asin(min(1,math.sqrt(math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))

def choose_region(data,city,countries):
    matches=[r for r in data.get('regions',[]) if r.get('name','').casefold()==city.casefold() and r.get('country') in countries and r.get('region_type')=='City']
    if len(matches)==1:return matches[0],'EXACT_CITY_COUNTRY_UNIQUE'
    precise=[r for r in matches if r.get('full_name') in [city+', '+country for country in countries]]
    if len(precise)==1:return precise[0],'EXACT_FULL_NAME_DISAMBIGUATED'
    place=data.get('place') or {};near=[]
    if all(type(place.get(k)) in (int,float) for k in ('latitude','longitude')):
        for r in matches:
            if all(type(r.get(k)) in (int,float) for k in ('latitude','longitude')) and distance_km(r,place)<=25:near.append(r)
    if len(near)==1:return near[0],'COUNTRY_FILTER_PLUS_RETURNED_PLACE_COORDINATES'
    return None,'AMBIGUOUS_OR_UNRESOLVED'
