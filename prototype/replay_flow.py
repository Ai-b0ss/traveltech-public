"""Connected replay of request -> captured tariffs -> refinement -> evidence."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'base'))
from dataclasses import asdict
from datetime import datetime,timedelta
from decimal import Decimal
from travel_core import Request,Guests,Engine
from current_adapter import CurrentCapture
from session import Session,State
from evidence import Evidence,verify_claim,explain
from fee_disclosure import disclosure

ROOT=Path(__file__).parent;OUT=ROOT/'results'
def main():
    rows=[]
    for city,country in [('Dubai','AE'),('Istanbul','TR')]:
        rates=json.loads((OUT/(city.lower()+'_sample_rates.json')).read_text());details=json.loads((OUT/(city.lower()+'_sample_detail.json')).read_text());adapter=CurrentCapture(rates,details,city,country)
        req=Request(city,country,'2026-10-26','2026-10-28',(Guests(2),),'100000','CNY');now=datetime.fromisoformat(rates['finished_at_utc'])
        offers=adapter.search(req);cheapest=min(offers,key=lambda o:Decimal(o.amount))
        session=Session(State(req,selected_offer=cheapest.identity));engine=Engine(adapter)
        before=session.recheck(engine,now);new_budget=str(max(Decimal('0'),Decimal(cheapest.amount)-1))
        preview=session.preview({'budget':new_budget},0);session.apply(preview);after=session.recheck(engine,now)
        assert before['selected_offer_status']=='REVIEW' and after['selected_offer_status']=='REJECT'
        session.undo(1);assert session.state.request==req
        deadline=cheapest.cancel_until
        evidence=[Evidence('rate_cancel',cheapest.hotel_id,'free_cancellation_now',bool(deadline and deadline>now),'RATE',now,cheapest.source_ref,room_id=cheapest.room_id,rate_id=cheapest.rate_id)]
        receipt=verify_claim(evidence,hotel_id=cheapest.hotel_id,aspect='free_cancellation_now',room_id=cheapest.room_id,rate_id=cheapest.rate_id,now=now,max_age=timedelta(hours=1))
        rows.append({'city':city,'selected_rate':cheapest.rate_id,'selected_room':cheapest.room_id,'source_products':len(offers),'initial_budget':req.budget,'selected_base_amount':cheapest.amount,'lowered_budget':new_budget,'selection_before':before['selected_offer_status'],'selection_after':after['selected_offer_status'],'dates_preserved':session.state.request.checkin==req.checkin,'guests_preserved':session.state.request.rooms==req.rooms,'revision_after_undo':session.state.revision,'fee_disclosure':disclosure(details),'cancel_claim':receipt,'cancel_claim_explanation':explain(receipt),'live_supplier_actions_during_replay':0,'natural_language_model_used':False})
    (OUT/'connected_replay.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    print(json.dumps([{'city':r['city'],'products':r['source_products'],'before':r['selection_before'],'after':r['selection_after'],'fee_status':r['fee_disclosure']['status']} for r in rows],ensure_ascii=False,indent=2))
if __name__=='__main__':main()
