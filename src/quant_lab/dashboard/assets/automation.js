/* Shared paper service status and broker navigation for both desks. */
(() => {
 'use strict';
 const labels={RUNNING:'自动交易运行中',WATCHING:'自动交易运行中',ENTRY_SUBMITTED:'已提交股票入场',OPTION_ENTRY_SUBMITTED:'已提交期权入场',EXIT_SUBMITTED:'正在退出持仓',WAIT_TRADING_DAY:'等待下一交易日',WAIT_REGULAR_SESSION:'等待常规交易时段',SESSION_COMPLETE:'本场已结束 · 自动等待下一场',MANUAL_STOP:'已手动停止',DISABLED:'自动交易未启用',RETRY_BACKOFF:'等待重启重试',EXECUTOR_STARTING_OR_STALE:'执行器启动或检查中',CONNECTION_OR_RECONCILIATION_HOLD:'连接或订单需要核对',RECONCILIATION_HOLD:'持仓或订单需要核对',OVERNIGHT_POSITION_HOLD:'隔夜持仓需要核对',ENTRY_WINDOW_CLOSED:'已停止新入场 · 继续管理退出',POSITION_LIMIT:'持仓名额已满 · 继续管理退出',DAILY_ENTRY_LIMIT:'今日次数已达上限 · 继续管理退出',EXTERNAL_ORDER_HOLD:'外部订单需要核对',POSITION_CHANGED:'持仓变化 · 正在核对'};
 const waiting=new Set(['WAIT_TRADING_DAY','WAIT_REGULAR_SESSION','SESSION_COMPLETE']);
 function show(name,row){
   const card=document.getElementById('automation-'+name);
   const label=!row.enabled?'自动交易未启用':row.stopped?'已手动停止':!row.fresh?'后台状态过期':labels[row.status]||'执行检查中';
   card.querySelector('strong').textContent=label;
   card.dataset.tone=!row.enabled||row.stopped?'neutral':!row.fresh||/HOLD|STALE/.test(row.status||'')?'warning':'active';
   card.querySelector('[data-auto-note]').textContent=row.next_open&&waiting.has(row.status)?'下次开盘 '+new Date(row.next_open).toLocaleString(window.QuantI18n?.locale||'zh-CN',{timeZone:'Pacific/Auckland',hour12:false})+'（奥克兰）':row.updated?'更新 '+new Date(row.updated).toLocaleTimeString(window.QuantI18n?.locale||'zh-CN',{hour12:false})+' · '+(waiting.has(row.status)?'无需手动启动':'模拟账户'):'等待后台连接';
 }
 async function refresh(){
   try{
     const response=await fetch('/api/automation',{cache:'no-store'});if(!response.ok)throw Error();
     const data=await response.json();
     const budget=data.budgets?.[location.pathname==='/moomoo.html'?'moomoo':'alpaca'];
     let budgetCard=document.getElementById('strategy-budget');
     if(budget&&!budgetCard){budgetCard=document.createElement('div');budgetCard.id='strategy-budget';budgetCard.className='notice-static';document.querySelector('main').prepend(budgetCard);}
     if(budgetCard){budgetCard.hidden=!budget;if(budget){const en=window.QuantI18n?.locale==='en',fmt=v=>Number(v??0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});budgetCard.textContent=budget.valid?(en?`Strategy capital $${fmt(budget.initial_capital)} · Equity $${fmt(budget.equity)} · Net P&L $${fmt(budget.net_pnl)} (${fmt(budget.return_pct)}%) · Estimated NZ stock fees $${fmt(budget.estimated_fees)}. Stocks only; old history retained separately.`:`策略本金 $${fmt(budget.initial_capital)} · 策略权益 $${fmt(budget.equity)} · 本轮净盈亏 $${fmt(budget.net_pnl)}（${fmt(budget.return_pct)}%）· NZ整股费用估算 $${fmt(budget.estimated_fees)}。本轮仅股票；旧历史单独保留。`):(en?'Strategy budget needs reconciliation; new entries blocked.':'策略预算需要核对，暂停新开仓。');}}
     document.getElementById('automation-service').textContent=data.status==='DEMO'?'演示模式 · 不会启动交易':data.fresh&&data.status==='RUNNING'?'自动守护在线 · 20秒检查': '自动守护未连接，请检查后台';
     show('alpaca',data.alpaca);show('moomoo',data.moomoo);
     document.getElementById('automation-startup').textContent=data.startup==='WINDOWS_LOGON'?'Windows 登录后自动启动；手动停止与当日风控锁定保留。':'电脑需在线；手动停止与当日风控锁定保留。';
   }catch{document.getElementById('automation-service').textContent='自动守护状态读取失败';}
 }
 const select=document.getElementById('broker-select');
 if(select&&location.pathname!=='/moomoo.html')select.addEventListener('change',()=>{if(select.value==='moomoo')location.href='/moomoo.html';});
 refresh();setInterval(refresh,10000);
})();
