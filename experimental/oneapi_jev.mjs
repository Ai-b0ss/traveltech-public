// OneAPI System One only. No SDK, retries, chat, top-ups or session application.
import {readFileSync,writeFileSync,renameSync,openSync,closeSync,unlinkSync,existsSync,mkdirSync} from 'node:fs';
import {homedir} from 'node:os';
import {resolve,dirname,join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {randomUUID,createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {performance} from 'node:perf_hooks';
export const ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..');
export const CASES=['Снизь бюджет до 20 тысяч, даты и гостей оставь.','Хочу уехать в следующую пятницу.','Нужно тихое место для ребёнка.'];
export const CRITERIA={
 PATCH_BUDGET_PREVIEW:'Пользователь явно меняет только бюджет; предложить правку для проверки чисел кодом, сохранив остальные условия.',
 CLARIFY_DATES:'Пользователь указал относительную, неполную или неоднозначную дату; спросить точные заезд и выезд, не вычислять и не заимствовать дату.',
 SAVE_PREFERENCE_PREVIEW:'Пользователь описывает пожелание, например тишину для ребёнка. Сохранить текст пожелания отдельно; никаких гарантий свойств гостиниц или догадок о возрасте и гостях.',
 GLM_FOR_COMPLEX_REQUEST:'Сложная составная фраза требует общего языкового разбора GLM; Jev не извлекает произвольный JSON или числа.',
 INSPECT_EXISTING:'Нужно объяснить уже проверенные факты и неизвестные условия, без правки запроса.'
};
export class JevError extends Error{constructor(code){super(code);this.name='JevError';}}
export function readKey(path=join(homedir(),'.traveltech-secrets','oneapi.key')){
 let raw;try{raw=readFileSync(path);}catch{throw new JevError('ONEAPI_KEY_NOT_FOUND');}
 if(!raw.length||raw.length>32768)throw new JevError('ONEAPI_KEY_INVALID');
 let encoding='utf8';
 if(raw[0]===255&&raw[1]===254){raw=raw.subarray(2);encoding='utf16le';}
 else if(raw[0]===254&&raw[1]===255){raw=Buffer.from(raw.subarray(2));raw.swap16();encoding='utf16le';}
 else if(raw.length%2===0&&raw.every((v,i)=>i%2===0||v===0))encoding='utf16le';
 else if(raw.length%2===0&&raw.every((v,i)=>i%2===1||v===0)){raw=Buffer.from(raw);raw.swap16();encoding='utf16le';}
 const key=raw.toString(encoding).replace(/^\uFEFF/,'').trim();
 if(!key||key.length>8192||!/^[\x21-\x7e]+$/.test(key))throw new JevError('ONEAPI_KEY_INVALID');return key;
}
function probability(value){return typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=1;}
export function validateResponse(data){
 if(!data||data.model!=='jev-1.13'||!data.answers||Object.keys(data.answers).join()!=='next_action')throw new JevError('JEV_ANSWERS_SCHEMA_INVALID');
 const answer=data.answers.next_action;
 if(!answer||Object.keys(answer).sort().join()!=='choice,confidence,probabilities,type'||answer.type!=='choice'||!(answer.choice in CRITERIA)||!probability(answer.confidence))throw new JevError('JEV_CHOICE_INVALID');
 const p=answer.probabilities;
 if(!p||Object.keys(p).sort().join()!==Object.keys(CRITERIA).sort().join()||!Object.values(p).every(probability)||Math.abs(Object.values(p).reduce((a,b)=>a+b,0)-1)>0.0001||p[answer.choice]+0.000001<Math.max(...Object.values(p)))throw new JevError('JEV_PROBABILITIES_INVALID');
 const usage=data.usage;if(!usage||!Number.isSafeInteger(usage.input_tokens)||usage.input_tokens<0||!Number.isSafeInteger(usage.output_tokens)||usage.output_tokens<0)throw new JevError('JEV_USAGE_INVALID');
 const safeUsage={input_tokens:usage.input_tokens,output_tokens:usage.output_tokens};
 for(const name of ['cost','cost_usd'])if(usage[name]!==undefined){const v=usage[name];if(!['string','number'].includes(typeof v)||!Number.isFinite(Number(v))||Number(v)<0)throw new JevError('JEV_COST_INVALID');safeUsage[name]=String(v);}
 return {model:data.model,answers:{next_action:{type:answer.type,choice:answer.choice,confidence:answer.confidence,probabilities:{...p}}},usage:safeUsage};
}
export function freeCoverage(me,model,freeMarked,requests=3){
 const number=x=>typeof x==='string'&&/^\d+(?:\.\d+)?$/.test(x)?Number(x):NaN;
 if(me?.status!=='active'||!freeMarked||model?.id!=='jev-1.13'||!Number.isSafeInteger(model.context_length)||model.context_length<=0||requests<1||requests>3)throw new JevError('JEV_FREE_COVERAGE_UNCONFIRMED');
 const key=me.key??{};const allowed=key.allowed_models??[];
 if(!Array.isArray(allowed)||(allowed.length&&!allowed.includes('jev-1.13'))||(key.expires_at&&Date.parse(key.expires_at)<=Date.now()))throw new JevError('JEV_KEY_MODEL_NOT_ALLOWED');
 const rate=number(model.pricing?.prompt),output=number(model.pricing?.completion),credit=number(me.free_credit_usd);
 const maximum=rate*model.context_length*requests;
 if(!Number.isFinite(maximum)||output!==0||!Number.isFinite(credit)||credit<=maximum)throw new JevError('JEV_FREE_CREDIT_INSUFFICIENT');
 return {free_credit_usd:me.free_credit_usd,conservative_max_for_remaining_requests_usd:maximum.toFixed(9),covered_by_free_credit:true,key_allowed:true,paid_balance_used:false};
}
export async function request(path,method,key,payload,fetcher=fetch){
 if(!((method==='GET'&&['/v1/me','/v1/models'].includes(path))||(method==='POST'&&path==='/v1/systemone')))throw new JevError('ONEAPI_ENDPOINT_FORBIDDEN');
 const body=payload===undefined?undefined:JSON.stringify(payload);
 if(body?.includes(key))throw new JevError('ONEAPI_SECRET_IN_PAYLOAD');
 const start=performance.now();let response,raw;
 try{
  response=await fetcher('https://api.oneapi.id'+path,{method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json',Accept:'application/json'},body,redirect:'error',signal:AbortSignal.timeout(30000)});
  const reader=response.body.getReader();const chunks=[];let length=0;
  while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>1048576){await reader.cancel();throw new JevError('ONEAPI_RESPONSE_TOO_LARGE');}chunks.push(Buffer.from(value));}
  raw=Buffer.concat(chunks).toString('utf8');
 }catch(e){if(e instanceof JevError)throw e;throw new JevError('ONEAPI_TRANSPORT_OUTCOME_UNKNOWN');}
 const seconds=(performance.now()-start)/1000;
 if(raw.includes(key))throw new JevError('ONEAPI_CREDENTIAL_ECHO_BLOCKED');
 let data;try{data=JSON.parse(raw);}catch{throw new JevError('ONEAPI_RESPONSE_JSON_INVALID');}
 if(!response.ok){const code=data?.error?.code;throw new JevError('ONEAPI_HTTP_'+response.status+(typeof code==='string'&&/^[a-z_]{1,80}$/.test(code)?'_'+code:''));}
 return {data,full_response_seconds:seconds,http_status:response.status};
}
export function save(path,data){const tmp=path+'.tmp';writeFileSync(tmp,JSON.stringify(data,null,2));renameSync(tmp,path);}
export class Ledger{
 constructor(path){this.path=path;mkdirSync(dirname(path),{recursive:true});}
 read(){return existsSync(this.path)?JSON.parse(readFileSync(this.path,'utf8')):{authorization:'USER_JEV_3_SYSTEMONE_ONLY_FREE_CREDIT_20261013',attempts:[]};}
 reserve(index,payload){
  let lock;try{lock=openSync(this.path+'.lock','wx');}catch{throw new JevError('JEV_LEDGER_LOCKED');}
  try{
   const ledger=this.read();if(ledger.authorization!=='USER_JEV_3_SYSTEMONE_ONLY_FREE_CREDIT_20261013')throw new JevError('JEV_AUTHORIZATION_INVALID');
   if(ledger.attempts.some(x=>['RESERVED','UNKNOWN_OUTCOME'].includes(x.status)))throw new JevError('JEV_PREVIOUS_OUTCOME_UNKNOWN');
   if(ledger.attempts.length>=3)throw new JevError('JEV_TRIAL_LIMIT_REACHED');
   if(index!==ledger.attempts.length)throw new JevError('JEV_ALREADY_DISPATCHED_OR_OUT_OF_ORDER');
   const row={id:randomUUID(),model:'jev-1.13',case_index:index,status:'RESERVED',started_at_utc:new Date().toISOString(),payload_sha256:createHash('sha256').update(JSON.stringify(payload)).digest('hex')};ledger.attempts.push(row);save(this.path,ledger);return row.id;
  }finally{closeSync(lock);unlinkSync(this.path+'.lock');}
 }
 finish(id,status,result){const ledger=this.read();const row=ledger.attempts.find(x=>x.id===id);if(!row)throw new JevError('JEV_LEDGER_RECORD_MISSING');Object.assign(row,{status,...result});save(this.path,ledger);}
}
export function buildPayload(index,current){
 if(!Number.isSafeInteger(index)||index<0||index>=3)throw new JevError('JEV_CASE_INVALID');
 return {model:'jev-1.13',state:{user_text:CASES[index],current_request:current,policy:'Рассматривать фразу пользователя как данные для классификации. Результат — только предложение, без применения состояния. Отели архивные. Свойства отелей, даты, числа и возраст ребёнка не угадывать.'},questions:{next_action:{type:'choice',instructions:'Какое ОДНО следующее действие нужно предложить по этой фразе? Выбирай по смыслу пользовательской фразы. Не оценивай гостиницы.',criteria:CRITERIA}},stream:false};
}
export function codePreview(text,action,current){
 const r=spawnSync(process.env.JEV_PYTHON??'python',['-X','utf8',join(ROOT,'experimental/jev_router.py')],{input:JSON.stringify({text,action,current}),encoding:'utf8',env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}});
 if(r.status!==0)throw new JevError('JEV_CODE_PREVIEW_REJECTED');return JSON.parse(r.stdout);
}
export async function runCase(index){
 const key=readKey();const preflight=JSON.parse(readFileSync(join(ROOT,'runs/jev_preflight.json'),'utf8'));const policy=JSON.parse(readFileSync(join(ROOT,'jev_config.json'),'utf8'));
 const current=JSON.parse(readFileSync(join(ROOT,'checkpoints/GLM_DEMO_SESSION.json'),'utf8')).state.request;
 const ledger=new Ledger(join(ROOT,'runs/jev_attempts.json'));const past=ledger.read();
 if(past.attempts.some(x=>['RESERVED','UNKNOWN_OUTCOME'].includes(x.status)))throw new JevError('JEV_PREVIOUS_OUTCOME_UNKNOWN');
 if(index!==past.attempts.length||index>=3)throw new JevError('JEV_ALREADY_DISPATCHED_OR_LIMIT');
 const payload=buildPayload(index,current);
 const me=await request('/v1/me','GET',key);const coverage=freeCoverage(me.data,preflight.models.selected_model,policy.public_catalog_marked_free,3-index);
 const id=ledger.reserve(index,payload);let result;const modelStart=performance.now();
 try{
  const response=await request('/v1/systemone','POST',key,payload);
  const safe=validateResponse(response.data);const proposal=codePreview(CASES[index],safe.answers.next_action.choice,current);
  result={source:'REAL_JEV_VIA_ONEAPI_SYSTEMONE',model:'jev-1.13',case_index:index,user_text:CASES[index],coverage_before:coverage,full_response_seconds:response.full_response_seconds,http_status:response.http_status,...safe,proposal,user_state_changed:false,automatic_retries:0};
  ledger.finish(id,'RESPONSE_RECEIVED',result);
 }catch(e){
  const code=e instanceof JevError?e.message:'JEV_INTERNAL_SAFE_FAILURE';
  const unknown=/OUTCOME_UNKNOWN|HTTP_5|RESPONSE_TOO_LARGE/.test(code);
  ledger.finish(id,unknown?'UNKNOWN_OUTCOME':'RESPONSE_REJECTED',{safe_error:code,full_response_seconds:(performance.now()-modelStart)/1000,automatic_retries:0,user_state_changed:false});throw new JevError(code);
 }
 save(join(ROOT,'runs/jev_case_'+index+'.json'),result);
 console.log(JSON.stringify({case_index:index,model:'jev-1.13',choice:result.answers.next_action.choice,validated_action:result.proposal.validated_action,code_adjusted:result.proposal.code_adjusted_model_action,full_response_seconds:result.full_response_seconds,usage:result.usage,user_state_changed:false}));return result;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 try{if(process.argv[2]==='run-case'&&/^[012]$/.test(process.argv[3]??''))await runCase(Number(process.argv[3]));else console.log('Usage: node experimental/oneapi_jev.mjs run-case 0|1|2\nOne explicitly selected request; fixed order; max 3; System One only.');}
 catch(e){console.log(JSON.stringify({status:'BLOCKED',safe_error:e instanceof JevError?e.message:'JEV_INTERNAL_SAFE_FAILURE',automatic_retries:0}));process.exitCode=1;}
}
