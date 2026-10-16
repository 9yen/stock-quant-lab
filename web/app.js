const closedReturn=v=>v==null?'—':(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const markerLabel=v=>({'开多':'买入·开多','平多':'卖出·平多','开空':'卖出·开空','回补':'买入·回补'})[v]||v;
'use strict';
const $=id=>document.getElementById(id), money=v=>Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
let latest=null,paused=false,connected=false,filter='all',frame=1,kind='candles',zoom=1,pan=0,hover=null,drag=null,lastEvent=0;
let maPeriods=[20,50];
let selectedSymbol='SPY',chartData=null,chartSequence=0,chartBusy=false,chartController=null,chartError=false;
function currentChart(){return chartData?.symbol===selectedSymbol?chartData:{symbol:selectedSymbol,bars:[],bar_minutes:1};}
const canvas=$('chart'),ctx=canvas.getContext('2d'),terminal=new Set(['filled','canceled','expired','rejected','replaced']);
function text(id,value){$(id).textContent=value;}
function localTime(t,zone='Pacific/Auckland'){return t?new Date(t).toLocaleString(window.QuantI18n?.locale||'zh-CN',{timeZone:zone,hour12:false}):'—';}
function finite(v){return Number.isFinite(Number(v));}
function render(data,fromStream=true){
 latest=data;if(fromStream)lastEvent=Date.now();
 if(paused)return;
 if(connected)text('connection',data.mode==='DEMO'?'DEMO · 合成数据':data.risk.allow_short?'LIVE · 多空双向':'LIVE · 只做多');
 const a=data.account,b=data.backend;
 text('chart-symbol',selectedSymbol);
 updateSymbolOptions(data.risk.universe||[]);
 renderScanner(data.scanner||{});

 text('bot-status',b.managed_wait?({WAIT_TRADING_DAY:'自动待机 · 等待交易日',WAIT_REGULAR_SESSION:'自动待机 · 等待开盘',SESSION_COMPLETE:'本场已结束 · 自动等待下一场',MANUAL_STOP:'已手动停止'})[b.auto_status]:b.label);text('bot-detail',b.demo?'合成数据 · 无账户连接，不会交易':b.managed_wait?(b.auto_status==='MANUAL_STOP'?'手动停止保留，自动守护不会覆盖':'已启用连续模拟交易；按官方日历自动启动'):b.completed?'本场已正常结束；下一交易日需重新授权并启动':b.healthy?'后台检查正常':b.alive?'后台心跳已过期，请检查':'交易进程未运行');
 $('bot-dot').style.background=b.managed_wait?'#90a4c0':b.completed?'#90a4c0':b.healthy?'#7be8b3':'#f48e8e';
 document.querySelectorAll('[data-risk-cap]').forEach(el=>el.textContent=(data.risk.cap*100).toFixed(0)+'%');
 text('exposure-policy',`新开仓目标 ${(data.risk.target_position*100).toFixed(0)}%；100只自选股票中最多同时持有 ${data.risk.max_positions} 只；单只最多 ${(data.risk.cap*100).toFixed(0)}%，组合多空名义敞口相加最多 ${(data.risk.gross_limit*100).toFixed(0)}%；包含未成交入场预留；同股先平仓再反向。`);
 text('session-date',data.session||'—');text('candidate-count',`${data.risk.candidates} / ${data.risk.max_candidates}`);
 text('direction-mode',data.risk.strategy==='multi_1m'?'100股 1m / 多空多仓 · 候选与持仓并发检查':data.risk.allow_short?'开盘区间突破 / 多空双向 · 一次一个方向':'开盘区间突破 / 只做多 SPY');
 text('filter-policy',(data.risk.entry_variant==='bounded_orb'?'V3试运行：开盘区间宽度≤3%，突破距离≤1.5×ATR14；全样本尚未验证盈利。':'')+(data.risk.trend_filter?'入场过滤已启用：MA20方向、VWAP同侧、当前分钟量不低于前20分钟均量；条件不足不入场。':'入场过滤未启用'));
 text('daily-loss-budget',(data.risk.strategy_daily_loss*100).toFixed(0)+'%');
 text('daily-loss-note',`按Alpaca本场基准净值计算，包含浮盈亏。达到 ${(data.risk.strategy_daily_loss*100).toFixed(0)}% 即锁定入场并尝试撤单平仓；绝对容忍上限 ${(data.risk.loss_limit*100).toFixed(0)}%，不等到该上限才退出。moomoo费用为独立估算。`);
 text('risk-status',data.risk.halted?'已触发本场风险锁定，停止新增入场。':b.stopped?'已请求停止，后台将尝试撤单和退出。':'风险限制已启用。当前网页不会修改交易参数。');
 if(b.stopped){$('stop').disabled=true;text('stop','已请求停止，等待确认');}
 const problems=[];
 if(data.account_error)problems.push('账户连接失败，以下为上次成功读取的数据');
 if(a&&Date.now()-Date.parse(a.fetched)>45000)problems.push('账户数据已超过 45 秒未更新');
 if(!b.healthy&&!b.completed&&!b.managed_wait)problems.push(b.alive?'后台心跳过期':'后台交易进程未运行');
 if(data.quote_error)problems.push('IEX 报价暂不可用');
 if(a?.chart_error)problems.push('K 线读取失败，等待下一次刷新');
 if(chartError)problems.push(selectedSymbol+' 图表更新失败，保留上次数据');
 $('error').hidden=!problems.length;text('error',problems.join(' · '));
 if(a){
  text('equity','$'+money(a.equity));text('baseline','本场基准 $'+money(a.baseline));
  text('pnl',(a.pnl>=0?'+':'−')+money(Math.abs(a.pnl)));$('pnl').className=a.pnl>=0?'positive':'negative';
  text('daily-pct',(a.daily_pct>=0?'+':'')+Number(a.daily_pct).toFixed(2)+'% · 含浮动盈亏');
  text('qty',(a.qty<0?'空头 ':a.qty>0?'多头 ':'')+Math.abs(Number(a.qty)).toLocaleString()+' 股');text('position-note','市值 $'+money(Math.abs(a.position_value))+' · 浮盈亏 '+money(a.position_pnl));
  if(data.risk.strategy==='multi_1m'){
   const holdings=a.holdings||[],gross=holdings.reduce((sum,p)=>sum+Math.abs(p.value),0);
   text('qty',holdings.length+' / '+data.risk.max_positions+' 只');text('position-note','名额占用 '+data.risk.slots_used+'/'+data.risk.max_positions+'（含未结束订单） · '+(gross/a.equity*100).toFixed(2)+'% 持仓敞口');
  }
  renderHoldings(a.holdings||[],a);
  renderRealized(a);
  renderHistory(a.review,a.review_error);
  renderDailyResults(a);
  const modelPct=a.review?.current?.model_return_pct;
  text('model-return',modelPct==null?'模型收益率暂不可用':`moomoo NZ 模型当日收益率 ${modelPct>=0?'+':''}${modelPct.toFixed(4)}%`);
  $('model-return').className=modelPct>=0?'positive':'negative';
  text('moomoo-fees',a.review?.current?money(a.review.current.fees):'—');
  text('moomoo-net',a.review?.current?money(a.review.current.closed_net):'—');
  text('cash','$'+money(a.cash));text('updated','更新于 '+new Date(a.fetched).toLocaleTimeString(window.QuantI18n?.locale||'zh-CN',{timeZone:'Pacific/Auckland',hour12:false}));
  text('chart-date',(currentChart().chart_date||'—')+' 美东');renderOrders(a.rows);
 }
 const q=currentChart().quote;
 if(q&&q.symbol===selectedSymbol&&finite(q.bid)&&finite(q.ask)&&q.bid>0&&q.ask>=q.bid){
  text('quote-price','$'+money((q.bid+q.ask)/2));
  text('bidask','买价 '+money(q.bid)+' / 卖价 '+money(q.ask));
 }else{text('quote-price','—');text('bidask','买价 — / 卖价 —');}
 renderEvents(data.events);draw();updateTime();
}
function renderOrders(rows=[]){
 const selected=rows.filter(r=>filter==='all'||(filter==='filled'?r.raw_status==='filled':!terminal.has(r.raw_status)));
 text('order-count',selected.length);$('orders').replaceChildren();
 if(!selected.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent='暂无符合条件的 SPY 订单';tr.append(td);$('orders').append(tr);return;}
 for(const r of selected){const tr=document.createElement('tr');
  for(const [i,v]of [(r.symbol||'SPY')+' / '+r.time,r.intent_label||r.side,r.qty,r.filled,r.price===null?'—':'$'+money(r.price),r.type,r.status].entries()){
   const td=document.createElement('td');td.textContent=v??'—';
   if(i===1)td.className=r.side==='买入'?'positive':'muted';
   if(i===6){td.replaceChildren();const badge=document.createElement('span');badge.className='order-badge'+(r.raw_status==='filled'?' filled':'');badge.textContent=r.status;td.append(badge);}tr.append(td);
  }$('orders').append(tr);
 }
}
function renderHoldings(holdings,a={}){
 const table=$('holdings');table.replaceChildren();
 for(const p of holdings){
  const realized=a.realized_complete?a.realized_by_symbol?.[p.symbol]:null;
  const pct=p.entry&&p.qty?p.pnl/(Math.abs(p.qty)*p.entry)*100:0;
  const values=[p.symbol,p.qty<0?'空头':'多头',Math.abs(p.qty),money(p.entry),'$'+money(Math.abs(p.value)),money(p.pnl),pct.toFixed(2)+'%',realized==null?'待对账':money(realized),realized==null?'—':money(realized+p.pnl)];
  const tr=document.createElement('tr');for(const [i,v]of values.entries()){const td=document.createElement('td');td.textContent=v;if(i>=5)td.className=(i===7?realized:i===8?realized+p.pnl:p.pnl)>=0?'positive':'negative';tr.append(td);}table.append(tr);
 }
 if(!holdings.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=9;td.className='empty';td.textContent='当前组合空仓；历史已实现盈亏见上方平仓明细';tr.append(td);table.append(tr);}
}
async function refreshChart(force=false){
 if(chartBusy&&!force)return;
 if(chartController)chartController.abort();
 const sequence=++chartSequence,symbol=selectedSymbol;chartBusy=true;chartController=new AbortController();
 try{const response=await fetch('/api/chart?symbol='+encodeURIComponent(symbol),{signal:chartController.signal});if(!response.ok)throw new Error('chart unavailable');const chart=await response.json();if(sequence!==chartSequence||symbol!==selectedSymbol)return;chartData=chart;chartError=false;if(latest)render(latest,false);}
 catch(error){if(error.name!=='AbortError'&&sequence===chartSequence){chartError=true;text('chart-date',symbol+' 行情暂不可用');text('quote-note','图表行情暂不可用');}}
 finally{if(sequence===chartSequence)chartBusy=false;}
}
function renderScanner(scan){
 const root=$('scanner-rows');root.replaceChildren();
 const age=scan.recorded_at?(Date.now()-Date.parse(scan.recorded_at))/1000:Infinity;
 const stale=age>90,labels={LONG:'向上突破 · 做多候选',SHORT:'向下突破 · 做空候选',WAIT:'等待',STALE:'过期',INVALID:'异常',INCOMPLETE:'数据不完整'};
 const active=latest?.risk?.strategy==='multi_1m';
 text('scanner-note',(stale?'扫描数据过期或尚未就绪':`更新于 ${localTime(scan.recorded_at)} · 20 秒轮询`)+ (active?' · 100股 1m 已接入统一模拟执行；原 ETF 只退出；下单前独立检查':' · 自动交易仍为 SPY 5m；此表只观察')+(scan.reason?' · '+scan.reason:''));
 for(const r of scan.rows||[]){const tr=document.createElement('tr');
  for(const [i,v]of [r.symbol,finite(r.price)&&r.price!==null?'$'+money(r.price):'—',stale?'过期':labels[r.minute_signal]||r.minute_signal,stale?'过期':labels[r.five_signal]||r.five_signal,r.shortable===null?'未知':r.shortable?'当前可借券':'当前不满足',`${latest?.risk?.per_symbol_entries?.[r.symbol]||0} / ${latest?.risk?.per_symbol_limit||19}`,`${r.opening_minutes}/15`,(active&&r.minute_signal==='LONG'&&!r.filter_long)||(active&&r.minute_signal==='SHORT'&&!r.filter_short)?'策略过滤：MA20/VWAP/量能未通过':r.reason].entries()){
   const td=document.createElement('td');td.textContent=i===7&&active&&String(v).includes('仅观察')?'执行前仍需即时报价与组合风控检查':v;
   if((i===2||i===3)&&!stale)td.className=(i===2?r.minute_signal:r.five_signal)==='LONG'?'positive':(i===2?r.minute_signal:r.five_signal)==='SHORT'?'negative':'';
   tr.append(td);
  }root.append(tr);
 }
 if(!(scan.rows||[]).length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=8;td.className='empty';td.textContent=scan.reason||'等待扫描数据';tr.append(td);root.append(tr);}
}
function renderEvents(events=[]){
 $('events').replaceChildren();
 for(const e of [...events].reverse().slice(0,30)){
  const row=document.createElement('div');row.className='event';
  for(const [tag,v]of [['time',localTime(e.recorded_at)],['strong',e.status||e.event],['span',e.reason||e.error_type||(e.qty?`${e.side||''} ${e.qty} 股`:e.event)]]){const node=document.createElement(tag);node.textContent=v||'—';row.append(node);}$('events').append(row);
 }
}
function barsForFrame(){
 const bars=(currentChart().bars||[]).filter(b=>['open','high','low','close'].every(k=>finite(b[k])));
 const base=currentChart().bar_minutes||1;
 if(frame<=base)return bars;
 const groups=new Map();
 const span=frame*60000,needed=frame/base;
 for(const b of bars){const t=Date.parse(b.time),key=Math.floor(t/span)*span;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(b);}
 return [...groups.values()].filter(g=>g.length===needed&&g.every((b,i)=>!i||Date.parse(b.time)-Date.parse(g[i-1].time)===base*60000)).map(g=>({time:g[0].time,open:g[0].open,high:Math.max(...g.map(b=>b.high)),low:Math.min(...g.map(b=>b.low)),close:g[g.length-1].close,volume:g.reduce((sum,b)=>sum+Number(b.volume||0),0)}));
}
function draw(){
 const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=rect.width,h=rect.height;
 if(w<10||h<10)return;
 canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 const all=barsForFrame(),count=Math.max(5,Math.ceil(all.length/zoom));
 pan=Math.max(0,Math.min(pan,Math.max(0,all.length-count)));const end=all.length-Math.round(pan),start=Math.max(0,end-count),bars=all.slice(start,end);
 ctx.font='10px Segoe UI';ctx.fillStyle='#81909c';
 if(!bars.length){ctx.textAlign='center';chartText(ctx,'等待可用的完整 K 线',w/2,h/2);return;}
 const span=Math.max(frame,currentChart().bar_minutes||1)*60000;
 const rawMarkers=(latest?.account?.trade_markers||[]).filter(m=>m.symbol===selectedSymbol&&Date.parse(m.time)>=Date.parse(bars[0].time)&&Date.parse(m.time)<Date.parse(bars[bars.length-1].time)+span);
 const groupedMarkers=new Map();
 for(const m of rawMarkers){const key=Math.floor(Date.parse(m.time)/span)+':'+m.label+':'+m.side;if(!groupedMarkers.has(key))groupedMarkers.set(key,{...m,qty:0,notional:0,count:0});const g=groupedMarkers.get(key);g.qty+=Number(m.qty);g.notional+=Number(m.qty)*m.price;g.count++;}
 const markers=[...groupedMarkers.values()].map(g=>({...g,price:g.notional/g.qty}));
 text('chart-window',new Date(bars[0].time).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'})+' – '+new Date(bars[bars.length-1].time).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'})+' 美东 · 拖动右移看更早');
 const maSeries=maPeriods.map(n=>all.map((b,i)=>i+1<n?null:all.slice(i+1-n,i+1).reduce((sum,r)=>sum+r.close,0)/n));
 const maVisible=maSeries.flatMap(series=>series.slice(start,end).filter(v=>v!==null));
 const L=12,R=w-64,T=18,B=h*.72,lo=Math.min(...bars.map(b=>b.low),...markers.map(m=>m.price),...maVisible),hi=Math.max(...bars.map(b=>b.high),...markers.map(m=>m.price),...maVisible),pad=Math.max((hi-lo)*.2,.15),min=lo-pad,max=hi+pad;
 const x=i=>L+(i+.5)*(R-L)/bars.length,y=p=>B-(p-min)/(max-min)*(B-T);
 ctx.textAlign='left';
 for(let i=0;i<5;i++){const p=min+(max-min)*i/4,yy=y(p);ctx.strokeStyle='#263039';ctx.setLineDash([2,5]);ctx.beginPath();ctx.moveTo(L,yy);ctx.lineTo(R,yy);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#71838e';chartText(ctx,p.toFixed(2),R+8,yy+3);}
 if(kind==='line'){
  const grad=ctx.createLinearGradient(0,T,0,B);grad.addColorStop(0,'#7be8b32e');grad.addColorStop(1,'#7be8b300');
  ctx.beginPath();bars.forEach((b,i)=>i?ctx.lineTo(x(i),y(b.close)):ctx.moveTo(x(i),y(b.close)));ctx.lineTo(x(bars.length-1),B);ctx.lineTo(x(0),B);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();bars.forEach((b,i)=>i?ctx.lineTo(x(i),y(b.close)):ctx.moveTo(x(i),y(b.close)));ctx.strokeStyle='#7be8b3';ctx.lineWidth=1.8;ctx.stroke();ctx.lineWidth=1;
 }else for(const [i,b]of bars.entries()){
  const color=b.close>=b.open?'#7be8b3':'#ed9991',bw=Math.max(2,Math.min(13,(R-L)/bars.length*.55)),xx=x(i);
  ctx.strokeStyle=color;ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(xx,y(b.high));ctx.lineTo(xx,y(b.low));ctx.stroke();ctx.fillRect(xx-bw/2,Math.min(y(b.open),y(b.close)),bw,Math.max(1,Math.abs(y(b.open)-y(b.close))));
 }
 const colors=['#e0c36e','#87a9ff','#d796eb'];
 maSeries.forEach((series,j)=>{ctx.strokeStyle=colors[j%colors.length];ctx.lineWidth=1.5;ctx.beginPath();let drawing=false;for(let i=0;i<bars.length;i++){const v=series[start+i];if(v===null){drawing=false;continue;}if(!drawing){ctx.moveTo(x(i),y(v));drawing=true;}else ctx.lineTo(x(i),y(v));}ctx.stroke();ctx.lineWidth=1;ctx.fillStyle=colors[j%colors.length];ctx.textAlign='left';chartText(ctx,'MA'+maPeriods[j],L+j*65,12);});
 const volumeTop=h*.80,volumeBottom=h-30,maxVolume=Math.max(1,...bars.map(b=>Number(b.volume||0)));
 ctx.strokeStyle='#263039';ctx.beginPath();ctx.moveTo(L,volumeTop-9);ctx.lineTo(R,volumeTop-9);ctx.stroke();ctx.textAlign='left';ctx.fillStyle='#81909c';chartText(ctx,'成交量 · IEX',L,volumeTop-13);
 for(const [i,b]of bars.entries()){const v=Number(b.volume||0);ctx.fillStyle=b.close>=b.open?'#4d9877':'#ad6d68';const bw=Math.max(1,(R-L)/bars.length*.65);ctx.fillRect(x(i)-bw/2,volumeBottom-v/maxVolume*(volumeBottom-volumeTop),bw,v/maxVolume*(volumeBottom-volumeTop));}
 // Actual fills, aligned to their containing candle and their executed price.
 const labelBoxes=[];
 for(const m of markers){const i=bars.findIndex(b=>Date.parse(m.time)>=Date.parse(b.time)&&Date.parse(m.time)<Date.parse(b.time)+span);if(i<0)continue;const xx=x(i),yy=y(m.price),buy=m.side==='buy';ctx.fillStyle=buy?'#67b7ff':'#ffb66b';ctx.beginPath();ctx.moveTo(xx,yy);ctx.lineTo(xx-5,yy+(buy?9:-9));ctx.lineTo(xx+5,yy+(buy?9:-9));ctx.closePath();ctx.fill();
  const label=markerLabel(m.label)+(m.count>1?' ×'+m.count:''),width=ctx.measureText(quantText(label)).width+8,left=Math.max(L,Math.min(R-width,xx-width/2));
  for(let lane=0;lane<2;lane++){const top=yy+(buy?14+lane*15:-26-lane*15);if(top<T||top+13>B)continue;const box={left,right:left+width,top,bottom:top+13};if(labelBoxes.some(p=>box.left<p.right+4&&box.right+4>p.left&&box.top<p.bottom+3&&box.bottom+3>p.top))continue;ctx.textAlign='left';chartText(ctx,label,left+4,top+10);labelBoxes.push(box);break;}
 }
 // Opening range uses the original three complete 5m bars, independent of zoom/timeframe.
 const opening=(currentChart().bars||[]).filter(b=>{const t=new Date(b.time).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour12:false});return t>='09:30:00'&&t<'09:45:00';});
 if(opening.length===15/(currentChart().bar_minutes||1))for(const [p,name]of [[Math.max(...opening.map(b=>b.high)),'区间上沿'],[Math.min(...opening.map(b=>b.low)),'区间下沿']]){
  if(p<min||p>max)continue;ctx.strokeStyle='#d7b776';ctx.setLineDash([5,5]);ctx.beginPath();ctx.moveTo(L,y(p));ctx.lineTo(R,y(p));ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#d7b776';chartText(ctx,name,L+5,y(p)-6);
 }
 ctx.fillStyle='#71838e';ctx.textAlign='center';
 for(const i of new Set([0,Math.floor(bars.length/3),Math.floor(bars.length*2/3),bars.length-1]))chartText(ctx,new Date(bars[i].time).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}),x(i),h-8);
 if(hover!==null&&hover>=L&&hover<=R){const i=Math.max(0,Math.min(bars.length-1,Math.floor((hover-L)/(R-L)*bars.length))),b=bars[i];
  ctx.strokeStyle='#82939e';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(x(i),T);ctx.lineTo(x(i),B);ctx.stroke();ctx.setLineDash([]);
  const tip=$('tooltip');tip.hidden=false;tip.style.left=Math.min(Math.max(x(i)+12,5),w-180)+'px';tip.style.top='8px';tip.textContent=new Date(b.time).toLocaleString(window.QuantI18n?.locale||'zh-CN',{timeZone:'America/New_York',hour12:false})+' 美东\n开 '+money(b.open)+'  高 '+money(b.high)+'\n低 '+money(b.low)+'  收 '+money(b.close)+'\nIEX成交量 '+Number(b.volume||0).toLocaleString()+maPeriods.map((n,j)=>'\nMA'+n+' '+(maSeries[j][start+i]===null?'预热不足':money(maSeries[j][start+i]))).join('')+markers.filter(m=>Date.parse(m.time)>=Date.parse(b.time)&&Date.parse(m.time)<Date.parse(b.time)+span).map(m=>'\n'+markerLabel(m.label)+' '+m.qty+' 股 · '+m.count+'笔 · 均价 '+money(m.price)+' '+new Date(m.time).toLocaleTimeString('en-GB',{timeZone:'America/New_York',hour12:false})).join('');
 }else $('tooltip').hidden=true;
}
function updateTime(){
 if(paused)return;
 const d=latest,a=d?.account,b=d?.backend,now=Date.now();
 const target=a?.market_open?a.close:(a?.next_open||b?.next_open);
 if(target){const sec=Math.max(0,Math.floor((Date.parse(target)-now)/1000)),h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;text('countdown',(a?.market_open?'距收盘 ':'距开盘 ')+[h,m,s].map(v=>String(v).padStart(2,'0')).join(':'));}
 const selectedQuote=currentChart().quote;
 if(selectedQuote){const age=Math.max(0,Math.floor((now-Date.parse(selectedQuote.timestamp))/1000));text('quote-note',(age<=60?'IEX 报价中间价 · ':'历史报价 · ')+(age<60?`${age} 秒前`:localTime(selectedQuote.timestamp)));$('quote-note').className=age<=60?'positive':'muted';}
 text('stream-time',connected?'已连接 · '+new Date(lastEvent).toLocaleTimeString(window.QuantI18n?.locale||'zh-CN',{hour12:false}):'连接已断开，正在重试');
 if(connected&&now-lastEvent>10000){text('connection','数据流过期');$('connection').className='pill connecting';}
}
document.querySelectorAll('[data-view]').forEach(btn=>btn.addEventListener('click',()=>{
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('selected',b===btn));
 for(const name of ['overview','orders','risk','history'])$(name+'-view').hidden=name!==btn.dataset.view;
 document.querySelectorAll('[data-page]').forEach(section=>section.hidden=section.dataset.page!==btn.dataset.view);
 text('page-title',btn.querySelector('span').textContent);window.scrollTo({top:0,behavior:'instant'});draw();
}));
document.querySelectorAll('[data-filter]').forEach(btn=>btn.addEventListener('click',()=>{filter=btn.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===btn));renderOrders(latest?.account?.rows);}));
document.querySelectorAll('[data-frame]').forEach(btn=>btn.addEventListener('click',()=>{frame=Number(btn.dataset.frame);zoom=1;pan=0;document.querySelectorAll('[data-frame]').forEach(b=>b.classList.toggle('active',b===btn));draw();}));
$('chart-type').addEventListener('click',()=>{kind=kind==='line'?'candles':'line';text('chart-type',kind==='line'?'折线':'K 线');draw();});
$('reset').addEventListener('click',()=>{zoom=1;pan=0;draw();});
$('pause').addEventListener('click',()=>{paused=!paused;text('pause',paused?'恢复显示':'暂停显示');text('connection',paused?'显示已暂停':connected?'LIVE':'重连中');$('connection').className=paused?'pill connecting':'pill';if(!paused&&latest)render(latest,false);});
$('full').addEventListener('click',()=>{const p=document.fullscreenElement?document.exitFullscreen():document.documentElement.requestFullscreen();p?.catch(()=>{});});
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(1,Math.min(12,zoom*(e.deltaY<0?1.2:1/1.2)));draw();},{passive:false});
canvas.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();canvas.style.cursor='grabbing';drag={x:e.clientX,pan};canvas.setPointerCapture(e.pointerId);});
canvas.addEventListener('pointermove',e=>{const rect=canvas.getBoundingClientRect();hover=e.clientX-rect.left;if(drag){pan=drag.pan+(e.clientX-drag.x)*barsForFrame().length/Math.max(1,zoom)/Math.max(1,rect.width-76);hover=null;}draw();});
canvas.addEventListener('pointerup',()=>{drag=null;canvas.style.cursor='crosshair';});canvas.addEventListener('pointercancel',()=>{drag=null;canvas.style.cursor='crosshair';});canvas.addEventListener('pointerleave',()=>{if(!drag){hover=null;draw();}});
new ResizeObserver(draw).observe(canvas);
$('stop').addEventListener('click',()=>$('stop-dialog').showModal());$('cancel-stop').addEventListener('click',()=>$('stop-dialog').close());
$('confirm-stop').addEventListener('click',async()=>{
 $('confirm-stop').disabled=true;
 try{const response=await fetch('/api/stop',{method:'POST',headers:{'X-Dashboard-Token':document.querySelector('meta[name="dashboard-token"]').content}});if(!response.ok)throw new Error('request failed');text('stop-message','请求已保存，等待后台确认撤单和持仓退出。');$('stop').disabled=true;text('stop','已请求停止');}
 catch{text('stop-message','请求未送达，请检查本机服务。');$('confirm-stop').disabled=false;}
});
const stream=new EventSource('/events');
stream.onopen=()=>{connected=true;if(!paused){text('connection','LIVE · 本机');$('connection').className='pill';}};
stream.onmessage=event=>{try{const data=JSON.parse(event.data);if(!paused){text('connection','LIVE · 本机');$('connection').className='pill';}render(data);}catch{ text('connection','数据异常');}};
stream.onerror=()=>{connected=false;if(!paused){text('connection','正在重连');$('connection').className='pill connecting';}};
setInterval(updateTime,1000);
$('chart-select').addEventListener('change',()=>{selectedSymbol=$('chart-select').value;chartData=null;chartError=false;zoom=1;pan=0;hover=null;text('chart-symbol',selectedSymbol);text('quote-price','—');text('bidask','买价 — / 卖价 —');text('chart-date','正在加载 '+selectedSymbol);text('quote-note','正在加载 '+selectedSymbol);draw();refreshChart(true);});
setInterval(()=>{if(!paused)refreshChart();},15000);
refreshChart();

function renderRealized(a){
 const signed=v=>(v>=0?'+':'−')+money(Math.abs(v));
 for(const [id,v] of [['realized',a.realized_pnl],['unrealized',a.unrealized_pnl]]){
  text(id,v===null||v===undefined?'—':signed(v));$(id).className=v>=0?'positive':'negative';
 }
 text('realized-note',a.realized_complete?`${a.realized_session} 美东 · FIFO · 未扣费用`:'成交记录未同步或读取失败，暂不显示金额');
 text('closed-note',`${a.closed_count||0} 笔成交配对 · 最多显示最近 100 笔 · 未扣费用`);
 const table=$('closed-trades');table.replaceChildren();
 for(const r of a.closed_rows||[]){const tr=document.createElement('tr');for(const [i,v] of [r.time,r.symbol,r.direction,r.qty,money(r.entry),money(r.exit),signed(r.pnl)].entries()){const td=document.createElement('td');td.textContent=v;if(i===6)td.className=r.pnl>=0?'positive':'negative';tr.append(td);}table.append(tr);}
 if(!(a.closed_rows||[]).length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=7;td.className='empty';td.textContent=a.realized_complete?'本场暂无平仓成交，已实现盈亏为 0；持仓盈亏显示在浮动盈亏中':'等待读取并核对成交记录';tr.append(td);table.append(tr);}
}

let historySelected='',historyStamp='',historyRecord=null;
function reviewRows(id,rows,columns){const table=$(id);table.replaceChildren();for(const r of rows){const tr=document.createElement('tr');for(const [i,col]of columns.entries()){const td=document.createElement('td');td.textContent=col(r);if((id==='daily-stock-results'&&[2,3,4,6,7].includes(i))||(id==='history-stocks'&&[1,2,7].includes(i))||(id==='history-trades'&&[6,8,9].includes(i))){const value=Number(td.textContent.replaceAll(',','').replace('%',''));if(Number.isFinite(value))td.className=value>=0?'positive':'negative';}tr.append(td);}table.append(tr);}if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=columns.length;td.className='empty';td.textContent='尚无符合条件的成交记录';tr.append(td);table.append(tr);}}
function renderHistory(review,error){
 if(!review){text('history-note',error?'复盘读取失败，保留上次成功记录；请检查连接':'等待实际成交记录');return;}
 const select=$('history-day'),days=review.days||[];
 if([...select.options].map(o=>o.value).join()!==days.map(r=>r.day).join()){select.replaceChildren();for(const r of days){const o=document.createElement('option');o.value=r.day;o.textContent=r.day+' 美东';select.append(o);}if(!days.some(r=>r.day===historySelected))historySelected=days[0]?.day||'';select.value=historySelected;}
 if(!historySelected){text('history-note','暂无成交日期');return;}
 const row=days.find(r=>r.day===historySelected),stamp=historySelected+(row?.updated||'');
 if(stamp===historyStamp)return;historyStamp=stamp;
 if(review.current?.day===historySelected){historyRecord=review.current;showHistory();return;}
 text('history-note','正在读取 '+historySelected+' 的已保存记录…');
 fetch('/api/review?day='+encodeURIComponent(historySelected)).then(r=>{if(!r.ok)throw Error();return r.json();}).then(r=>{if(r.day===historySelected){historyRecord=r;showHistory();}}).catch(()=>{historyStamp='';text('history-note','历史记录暂不可用，等待下次刷新');});
}
function showHistory(){const r=historyRecord;if(!r)return;
 text('history-note',r.day+' 美东 · '+r.status+(r.reconciled?' · 成交与持仓已核对':' · 成交尚待核对')+' · 更新 '+localTime(r.updated)+(r.valuation_time?' · 浮盈亏采样 '+localTime(r.valuation_time):' · 无历史浮盈亏采样，合计暂缺'));
 for(const [id,v]of [['history-net',r.closed_net],['history-fees',r.fees],['history-day-net',r.realized_less_day_fees],['history-total',r.net_with_unrealized]])text(id,v==null?'—':money(v));
 text('history-return',r.model_return_pct==null?'本日缺少基准净值，暂不计算收益率':`模型当日收益率 ${r.model_return_pct.toFixed(4)}%（包含最近记录浮盈，非落袋收益）；基准 $${money(r.baseline)}。`);
 text('history-ranking',r.best?(r.best===r.worst?`当前只有 ${r.best} 有平仓结果，净盈亏 ${money((r.stocks||[]).find(s=>s.symbol===r.best)?.closed_net||0)}；其他股票尚无可比较的平仓结果。`:`按已平仓净盈亏：最好 ${r.best}，最差 ${r.worst}；未平仓股票不参加排名。`):'尚无平仓成交，暂不评选最好/最差；浮盈不算已实现收益。');
 const f=r.profile||{};text('history-profile',(f.name||'')+' · '+(f.rounding||'')+'。'+(f.exclusions||''));
 reviewRows('history-stocks',r.stocks||[],[s=>s.symbol,s=>money(s.gross_realized),s=>s.closed_batches?money(s.closed_net):'尚无平仓',s=>money(s.fees),s=>s.closed_batches,s=>s.orders,s=>s.end_qty||0,s=>closedReturn(s.net_return_pct)]);
 const trades=[...(r.trades||[])].sort((a,b)=>$('history-sort').value==='worst'?a.net-b.net:b.net-a.net);
 reviewRows('history-trades',trades,[t=>localTime(t.time,'America/New_York'),t=>t.symbol,t=>t.direction,t=>t.qty,t=>money(t.entry),t=>money(t.exit),t=>money(t.gross),t=>money(t.fees),t=>money(t.net),t=>closedReturn(t.net_return_pct)]);
}
$('history-day').addEventListener('change',()=>{historySelected=$('history-day').value;historyStamp='';renderHistory(latest?.account?.review);});
$('history-sort').addEventListener('change',showHistory);

function updateSymbolOptions(symbols){const select=$('chart-select');if([...select.options].map(o=>o.value).join()===symbols.join())return;select.replaceChildren();$('chart-symbols').replaceChildren();for(const symbol of symbols){const o=document.createElement('option');o.value=symbol;o.textContent=symbol;select.append(o);const d=document.createElement('option');d.value=symbol;d.label=(latest?.scanner?.rows||[]).find(r=>r.symbol===symbol)?.name||symbol;$('chart-symbols').append(d);}select.value=selectedSymbol;}
function selectChartSearch(){const symbol=$('chart-search').value.trim().toUpperCase();if([...$('chart-select').options].some(o=>o.value===symbol)){$('chart-select').value=symbol;$('chart-select').dispatchEvent(new Event('change'));}else text('chart-date','请选择股票池中的有效代码');}
$('chart-search').addEventListener('change',selectChartSearch);
$('search-chart').addEventListener('click',selectChartSearch);
$('chart-search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();selectChartSearch();}});
function applyMA(){const raw=$('ma-periods').value.trim(),values=raw?raw.split(/[,，\s]+/).map(Number):[];if(values.length>3||values.some(n=>!Number.isInteger(n)||n<2||n>500)){text('ma-note','最多3条，周期为2–500的整数；留空关闭');return;}maPeriods=[...new Set(values)];text('ma-note',maPeriods.length?maPeriods.map(n=>'MA'+n).join(' / ')+' · 按当前图表有数据K线计算':'均线已关闭');try{localStorage.setItem('quant-ma',maPeriods.join(','));}catch{}draw();}
$('apply-ma').addEventListener('click',applyMA);try{const saved=localStorage.getItem('quant-ma');if(saved!==null){$('ma-periods').value=saved;applyMA();}}catch{}

function renderDailyResults(a){const rows=(a.review?.current?.stocks||[]).map(s=>({...s,floating:(a.holdings||[]).find(p=>p.symbol===s.symbol)?.pnl||0,held:(a.holdings||[]).some(p=>p.symbol===s.symbol)}));rows.sort((a,b)=>(b.closed_batches>0)-(a.closed_batches>0)||b.closed_net-a.closed_net);reviewRows('daily-stock-results',rows,[s=>s.symbol,s=>s.held?(s.closed_batches?'部分平仓/仍持有':'持仓中'):'已平仓',s=>money(s.gross_realized),s=>s.closed_batches?money(s.closed_net):'尚无平仓',s=>money(s.floating),s=>money(s.fees),s=>money(s.gross_realized+s.floating-s.fees),s=>closedReturn(s.net_return_pct)]);}

function shiftChart(direction){const visible=Math.max(5,Math.ceil(barsForFrame().length/zoom));pan+=direction*Math.max(1,Math.round(visible*.7));hover=null;draw();}
$('older-bars').addEventListener('click',()=>shiftChart(1));$('newer-bars').addEventListener('click',()=>shiftChart(-1));

function quantText(value){return window.QuantI18n?.t(value)??value;}
function chartText(context,value,...coordinates){context.fillText(quantText(value),...coordinates);}
window.addEventListener("quant:locale",()=>{draw();});
