/* Shared optional views. Capabilities never authorize a broker action. */
(() => {
 'use strict';
 const byId=id=>document.getElementById(id),moo=location.pathname==='/moomoo.html';
 let state=null;
 const text=(id,value)=>{const el=byId(id);if(el)el.textContent=value??'—';};
 const money=v=>v==null?'—':Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
 function table(id,rows,columns){const target=byId(id);target.replaceChildren();for(const row of rows){const tr=document.createElement('tr');for(const get of columns){const td=document.createElement('td');td.textContent=get(row)??'—';tr.append(td);}target.append(tr);}if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=columns.length;td.className='empty';td.textContent='暂无符合条件的数据';tr.append(td);target.append(tr);}}
 function render(){
  text('desk-option-availability',moo?'期权功能取决于独立账户权限与本地授权；当前页面不会启用交易。':'Alpaca 期权执行未接入本项目；股票功能正常。');
  byId('option-details').hidden=!moo;
  const enabled=state?.capabilities?.options_enabled;
  text('options-badge',enabled?'SIMULATE · 已启用':'未启用');
  const watch=state?.extensions?.options_watch||{};
  text('option-watch-note',watch.ok?'合约观察 · 以报价更新时间为准':'期权观察数据暂不可用');
  table('option-watch',watch.rows||[],[r=>r.underlying?.replace('US.',''),r=>r.type,r=>r.expiry,r=>money(r.strike),r=>money(r.bid)+' / '+money(r.ask),r=>money(r.contract_cost),r=>r.open_interest+' / '+r.volume,r=>r.time]);
  table('option-orders',(state?.account?.rows||[]).filter(r=>r.instrument==='OPTION'),[r=>r.time,r=>r.symbol,r=>r.intent_label,r=>r.filled+' / '+r.qty,r=>money(r.price),r=>r.status]);
  drawEquity();
 }
 function drawEquity(){
  const canvas=byId('equity-chart');if(!canvas||!canvas.clientWidth)return;
  const ctx=canvas.getContext('2d'),w=canvas.clientWidth,h=160,dpr=window.devicePixelRatio||1;
  canvas.width=w*dpr;canvas.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  const rows=(state?.extensions?.equity_curve||[]).filter(r=>Number.isFinite(r.equity));
  text('equity-note',rows.length?'本机采样 '+rows.length+' 点':'等待净值采样');
  if(!rows.length)return;
  const low=Math.min(...rows.map(r=>r.equity)),high=Math.max(...rows.map(r=>r.equity)),range=Math.max(high-low,1);
  ctx.beginPath();ctx.strokeStyle='#7be8b3';rows.forEach((r,i)=>{const x=15+i*(w-115)/Math.max(1,rows.length-1),y=135-(r.equity-low)/range*110;if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();ctx.fillStyle='#81909c';ctx.font='11px sans-serif';ctx.fillText('$'+money(high),w-95,25);ctx.fillText('$'+money(low),w-95,135);
 }
 async function optimizer(){try{
  const response=await fetch('/api/optimizer',{cache:'no-store'});if(!response.ok)throw Error();const data=await response.json();
  text('optimizer-status',data.status||'等待评估');text('optimizer-message',data.message||'样本不足时保持现行参数');
  text('optimizer-days',(data.valid_days??0)+' / '+(data.minimum_days??30));text('optimizer-closes',(data.live_stock_closes??0)+' / '+(data.minimum_live_closes??100));
  text('optimizer-profile',data.profile||'baseline');text('optimizer-next',data.pending_profile?data.pending_profile+' / '+data.effective_from:'保持不变');text('optimizer-updated',data.updated||'—');text('optimizer-limitations',data.limitations||'该报告用于参数研究，不保证收益。');
  table('optimizer-results',Object.entries(data.results||{}).map(([name,value])=>({name,...value})),[r=>r.name,r=>money(r.train?.net),r=>money(r.validation?.net),r=>money(r.holdout?.net),r=>r.holdout?.drawdown==null?'—':(r.holdout.drawdown*100).toFixed(2)+'%',r=>money(r.stress?.net)]);
 }catch{text('optimizer-status','评估报告暂不可用');}}
 window.addEventListener('quant:desk',event=>{state=event.detail;render();});
 window.addEventListener('quant:locale',render);new ResizeObserver(drawEquity).observe(byId('equity-chart'));
 document.querySelector('[data-view="optimize"]').addEventListener('click',optimizer);
 render();optimizer();
})();
