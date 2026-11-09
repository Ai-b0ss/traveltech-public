"""A corrected Moscow search and a bounded sample of dated room/detail reads."""
import json,concurrent.futures
from pathlib import Path
from probe_sources import call,OUT
from resolve_region import choose_region

def one_city(city,country):
    search=json.loads((OUT/(city.lower()+'_search.json')).read_text())
    hotels=(search.get('response') or {}).get('data',{}).get('hotels',[])
    if not hotels:return {'city':city,'status':'NO_CANDIDATES_FOR_SAMPLE'}
    hotel=hotels[0];hotel_id=str(hotel['hotel_id'])
    # One representative candidate, not a claim about the best hotel.
    payload={k:v for k,v in search['request'].items() if k in ['check_in_date','check_out_date','adults','room_count','children','children_ages']}
    payload['hotel_id']=hotel_id
    rate=call('query_room_rates',payload,city.lower()+'_sample_rates.json')
    detail=call('get_hotel_detail',{'hotel_id':hotel_id},city.lower()+'_sample_detail.json',20)
    data=(rate.get('response') or {}).get('data') or {};rooms=data.get('room_types',[])
    return {'city':city,'country':country,'hotel_id':hotel_id,'hotel_name':hotel.get('hotel_name'),'sample_selection':'FIRST_RETURNED_CANDIDATE_NOT_BEST_RECOMMENDATION','rates_status':rate['status'],'rates_ok':(rate.get('response') or {}).get('ok'),'room_types':len(rooms),'rate_products':sum(len(r.get('products',[])) for r in rooms),'rates_seconds':rate['elapsed_seconds'],'detail_status':detail['status'],'detail_ok':(detail.get('response') or {}).get('ok'),'detail_seconds':detail['elapsed_seconds'],'booking_or_inventory_independently_verified':False}

def main():
    loc=json.loads((OUT/'live_access_attempt.json').read_text());region,reason=choose_region(loc['response']['data'],'Moscow',['Russia'])
    if region is None:raise ValueError('MOSCOW_STILL_AMBIGUOUS')
    payload={'region_id':str(region['region_id']),'location_name':'Moscow','check_in_date':'2026-10-26','check_out_date':'2026-10-28','adults':2,'room_count':1,'children':0,'children_ages':[]}
    search=call('search_hotels',payload,'moscow_corrected_search.json')
    summary={'city':'Moscow','region_id':str(region['region_id']),'resolution':reason,'http_status':search.get('http_status'),'response_ok':(search.get('response') or {}).get('ok'),'error':(search.get('response') or {}).get('error'),'seconds':search['elapsed_seconds']}
    (OUT/'moscow_corrected_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary),flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(one_city,'Dubai','AE'),pool.submit(one_city,'Istanbul','TR')];results=[]
        for f in concurrent.futures.as_completed(futures):
            r=f.result();results.append(r);print(json.dumps(r,ensure_ascii=False),flush=True)
    (OUT/'selected_sample_summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
