"""Replay of freshly captured supplier responses, not an on-demand live connector."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'base'))
from datetime import datetime
from decimal import Decimal
from travel_core import Offer,Guests,SourceError

class CurrentCapture:
    def __init__(self,rate_capture,detail_capture,city,country):
        self.capture=rate_capture;self.detail=detail_capture;self.city=city;self.country=country
    def search(self,request):
        cap=self.capture;payload=cap['request'];response=cap.get('response') or {};detail=self.detail.get('response') or {}
        if response.get('ok') is not True or detail.get('ok') is not True:raise SourceError('CAPTURE_NOT_SUCCESSFUL')
        hotel=detail['data']['hotel']
        if str(hotel.get('hotel_id'))!=str(payload['hotel_id']) or hotel.get('country_code')!=self.country:raise SourceError('HOTEL_COUNTRY_BINDING_FAILED')
        if type(payload.get('adults')) is not int or type(payload.get('room_count')) is not int:raise SourceError('INVALID_CAPTURE_GUESTS')
        children=payload.get('children_ages',[])
        if len(children)!=payload.get('children',0):raise SourceError('CHILD_COUNT_MISMATCH')
        party=tuple(Guests(payload['adults'],tuple(children)) for _ in range(payload['room_count']))
        start=payload['check_in_date'];end=payload['check_out_date'];observed=datetime.fromisoformat(cap['finished_at_utc']);offers=[]
        for room in response['data'].get('room_types',[]):
            for product in room.get('products',[]):
                rate=product['rate'];fee=rate.get('stripe_payment_fee') or {};policy=product.get('cancellation_policy') or {};deadline=policy.get('free_cancel_deadline')
                amount=str(rate['total_price'])
                if fee:
                    if fee.get('currency')!=rate['currency'] or Decimal(str(fee['payable_amount']))!=Decimal(amount)+Decimal(str(fee['fee_amount'])):raise SourceError('FEE_ARITHMETIC_OR_CURRENCY_MISMATCH')
                offers.append(Offer('TOURMIND',str(payload['hotel_id']),str(room['room_type_code']),str(rate['rate_code']),self.city,self.country,start,end,party,rate['currency'],amount,'FULL_STAY_ALL_ROOMS',None,'PROVIDER_RESPONSE',observed,cap['endpoint']+'#'+str(rate['rate_code']),breakfast=None,cancel_until=datetime.fromisoformat(deadline) if deadline and policy.get('effective_non_refundable') is not True and policy.get('type')=='free_cancel_before_deadline' else None,capacity=product.get('max_occupancy'),fee=str(fee['fee_amount']) if fee else None,fee_method='STRIPE' if fee else None))
        return offers
