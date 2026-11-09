"""Four additional read-only samples: children and two equal-occupancy rooms."""
import json,concurrent.futures
from probe_sources import call,OUT

def sample(city,mode):
    previous=json.loads((OUT/(city+'_sample_rates.json')).read_text())
    payload=dict(previous['request'])
    if mode=='child6':payload.update(adults=2,room_count=1,children=1,children_ages=[6])
    else:payload.update(adults=1,room_count=2,children=0,children_ages=[])
    row=call('query_room_rates',payload,city+'_'+mode+'_rates.json')
    response=row.get('response') or {};data=response.get('data') or {};rooms=data.get('room_types',[])
    outcome='SOURCE_FAILURE' if row['status']!='HTTP_RESPONSE_JSON' or response.get('ok') is not True else 'MATCHED_PRODUCTS' if rooms else 'NO_MATCHING_PRODUCTS'
    return {'city':city,'mode':mode,'request':payload,'ok':response.get('ok'),'transport_status':row['status'],'http_status':row.get('http_status'),'outcome':outcome,'reason':data.get('reason') or response.get('error'),'room_types':len(rooms),'rate_products':sum(len(r.get('products',[])) for r in rooms),'seconds':row['elapsed_seconds'],'source_reported_only':True}

def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(sample,c,m) for c in ['dubai','istanbul'] for m in ['child6','two_rooms']];rows=[]
        for future in concurrent.futures.as_completed(tasks):
            row=future.result();rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    (OUT/'occupancy_probe_summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
