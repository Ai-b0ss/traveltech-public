"""Bounded public read-only TourMind research. No credentials or order tools.

Default: 5 new location calls + 6 dated hotel searches. One prior Moscow
location response is reused. No automatic retries, no hidden paid credentials.
"""
import json,time,urllib.request,urllib.error,concurrent.futures
from datetime import datetime,timezone
from pathlib import Path

BASE='https://api.tourmind.com/skill/toc/'
ALLOWED={'search_location','search_hotels','query_room_rates','get_hotel_detail'}
OUT=Path(__file__).parent/'results';OUT.mkdir(exist_ok=True)
MAX_BYTES=1048576

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def call(op,payload,filename,timeout=45):
    if op not in ALLOWED:raise ValueError('OPERATION_NOT_ALLOWED')
    if 'user_key' in payload:raise ValueError('CREDENTIALS_NOT_ALLOWED')
    row={'operation':op,'endpoint':BASE+op,'request':payload,'started_at_utc':datetime.now(timezone.utc).isoformat(),'attempts':1,'credentials_sent':False,'booking_or_payment':False}
    started=time.monotonic()
    req=urllib.request.Request(BASE+op,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','User-Agent':'TravelTech-Research/0.3'},method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=timeout) as response:
            body=response.read(MAX_BYTES+1);row['http_status']=response.status
            if len(body)>MAX_BYTES:row['status']='RESPONSE_TOO_LARGE'
            else:
                try:row.update(status='HTTP_RESPONSE_JSON',response=json.loads(body))
                except ValueError:row.update(status='NON_JSON_RESPONSE',body_excerpt=body[:1000].decode('utf-8','replace'))
    except urllib.error.HTTPError as e:row.update(status='HTTP_ERROR',http_status=e.code,body_excerpt=e.read(1000).decode('utf-8','replace'))
    except Exception as e:row.update(status='TRANSPORT_ERROR',exception=type(e).__name__,reason=str(e)[:500])
    row['elapsed_seconds']=round(time.monotonic()-started,3)
    row['finished_at_utc']=datetime.now(timezone.utc).isoformat()
    (OUT/filename).write_text(json.dumps(row,ensure_ascii=False,indent=2));return row

CITIES=[('Moscow',['Russia'],'RU'),('Sochi',['Russia'],'RU'),('Kazan',['Russia'],'RU'),('Dubai',['United Arab Emirates'],'AE'),('Istanbul',['Turkey','Türkiye'],'TR'),('Rome',['Italy'],'IT')]

def probe_city(item):
    city,countries,country_code=item;key=city.lower()
    previous=OUT/'live_access_attempt.json'
    if city=='Moscow' and previous.exists():
        loc=json.loads(previous.read_text());loc['request']=loc.get('payload',{})
    else:loc=call('search_location',{'keyword':city},key+'_location.json',20)
    regions=(loc.get('response') or {}).get('data',{}).get('regions',[])
    matching=[r for r in regions if r.get('name','').casefold()==city.casefold() and r.get('country') in countries and r.get('region_type')=='City']
    if len(matching)!=1:
        return {'city':city,'country':country_code,'status':'REGION_UNRESOLVED','matching_regions':len(matching),'location_status':loc['status'],'hotel_count_not_inventory':[r.get('hotel_count') for r in matching]}
    region=matching[0]
    payload={'region_id':str(region['region_id']),'location_name':region['name'],'check_in_date':'2026-10-26','check_out_date':'2026-10-28','adults':2,'room_count':1,'children':0,'children_ages':[]}
    hotels=call('search_hotels',payload,key+'_search.json')
    response=hotels.get('response') or {};data=response.get('data') or {}
    candidates=data.get('hotels',[]) if isinstance(data,dict) else []
    return {'city':city,'country':country_code,'region_id':str(region['region_id']),'status':'SEARCH_RESPONDED' if response.get('ok') is True else 'SEARCH_NO_SUCCESS','location_status':loc['status'],'search_transport':hotels['status'],'search_error':response.get('error'),'candidate_count':len(candidates),'search_seconds':hotels['elapsed_seconds'],'location_seconds':loc['elapsed_seconds'],'candidates':candidates,'search_capture':key+'_search.json','dated_rates_verified':False,'fees_rights_and_sla_verified':False}

def main():
    # Bound concurrency and preserve ordered outcomes independently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(probe_city,item):item[0] for item in CITIES};results=[]
        for future in concurrent.futures.as_completed(futures):
            row=future.result();results.append(row)
            print(json.dumps({k:v for k,v in row.items() if k!='candidates'},ensure_ascii=False),flush=True)
            (OUT/'regional_probe_progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    (OUT/'regional_probe_summary.json').write_text(json.dumps(sorted(results,key=lambda r:r['city']),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
