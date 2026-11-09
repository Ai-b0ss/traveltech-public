from html.parser import HTMLParser
class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
        if tag in ('p','li','br','ul'):self.parts.append(' ')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.hidden:self.hidden-=1
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)
def plain(text):
    p=PlainText();p.feed(text);return ' '.join(''.join(p.parts).split())
def disclosure(detail_capture):
    hotel=detail_capture['response']['data']['hotel'];fees=hotel.get('fees') or {}
    mandatory=plain(fees.get('mandatory') or '');optional=plain(fees.get('optional') or '')
    return {'hotel_id':str(hotel['hotel_id']),'status':'MANDATORY_FEES_REQUIRE_REVIEW' if mandatory else 'NO_MANDATORY_TEXT_IN_CAPTURE_NOT_PROOF_OF_NONE','mandatory_text':mandatory,'optional_text':optional,'total_extra_fee_calculated':False,'reason':'Text may repeat the same charge or use another currency/charging basis; no blind numeric summation','source_ref':detail_capture['endpoint'],'observed_at':detail_capture['finished_at_utc']}
