const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../src/quant_lab/dashboard');
const en=JSON.parse(fs.readFileSync(path.join(root,'assets/locales/en.json'),'utf8'));
const zh=JSON.parse(fs.readFileSync(path.join(root,'assets/locales/zh-CN.json'),'utf8'));

function runtime(){
  const text={nodeType:3,nodeValue:'账户净值',parentElement:{closest(){return null;}}};
  const body={nodeType:1,getAttribute(){return null;},closest(){return null;}};
  const select={value:'zh-CN',addEventListener(){}};
  const saved=new Map();
  const context={navigator:{language:'zh-CN'},localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},
    window:{dispatchEvent(){}},Event:class{},Node:{TEXT_NODE:3,ELEMENT_NODE:1},NodeFilter:{SHOW_ELEMENT:1,SHOW_TEXT:4},
    MutationObserver:class{constructor(cb){this.callback=cb;}observe(){}disconnect(){}},
    document:{body,documentElement:{lang:'zh-CN'},getElementById:()=>select,querySelector:()=>body,
      createTreeWalker(){let used=false;return{nextNode(){if(used)return null;used=true;return text;}};}},
    fetch:async url=>({ok:true,json:async()=>url.includes('zh-CN')?zh:en})};
  vm.runInNewContext(fs.readFileSync(path.join(root,'assets/i18n.js'),'utf8'),context);
  return {api:context.window.QuantI18n,context,text,select,saved};
}

test('catalogs have identical keys and preserve paper mode terminology',()=>{
  assert.deepEqual(Object.keys(en).sort(),Object.keys(zh).sort());
  assert.ok(Object.keys(en).length>=500);
  assert.match(en['仅模拟账户 · 不使用真实资金'],/Paper account only/);
  assert.match(en['模拟资金 · 本机运行'],/Paper/);
});

test('language switches round-trip without changing source text or numeric values',async()=>{
  const r=runtime();
  assert.equal(await r.api.setLocale('en'),true);
  assert.equal(r.text.nodeValue,'Account equity');
  assert.equal(r.api.t('SMCI · 已平仓 +12.34 USD'),'SMCI · Closed +12.34 USD');
  assert.equal(r.api.t('1.00% / USD 123.45'),'1.00% / USD 123.45');
  assert.equal(r.api.t('moomoo NZ 模型当日收益率 +1.2345%'),'moomoo NZ modeled daily return +1.2345%');
  assert.equal(r.api.t('按已平仓净盈亏：最好 AAPL，最差 MSFT；未平仓股票不参加排名。'),'By closed net P&L: best AAPL, worst MSFT; open positions are excluded from ranking.');
  assert.equal(r.api.t('全新且未翻译的券商提示'),'全新且未翻译的券商提示');
  assert.equal(r.saved.get('quant-lab-locale'),'en');
  await r.api.setLocale('zh-CN');
  assert.equal(r.text.nodeValue,'账户净值');
  assert.equal(r.context.document.documentElement.lang,'zh-CN');
});

test('unknown locales and failed fetches preserve the active language',async()=>{
  const r=runtime();await r.api.setLocale('en');
  assert.equal(await r.api.setLocale('../../.env'),false);
  r.context.fetch=async()=>{throw Error('offline');};
  assert.equal(await r.api.setLocale('zh-CN'),false);
  assert.equal(r.api.locale,'en');
});

test('a late locale response cannot overwrite a newer selection',async()=>{
  const r=runtime();await r.api.setLocale('en');
  let resolveSlow;
  r.context.fetch=url=>url.includes('zh-CN')?new Promise(resolve=>{resolveSlow=resolve;}):Promise.resolve({ok:true,json:async()=>en});
  const slow=r.api.setLocale('zh-CN');
  await r.api.setLocale('en');
  resolveSlow({ok:true,json:async()=>zh});
  assert.equal(await slow,false);
  assert.equal(r.api.locale,'en');
});

test('both pages offer the same language selector and all static Chinese copy is covered',()=>{
  const keys=Object.keys(en).sort((a,b)=>b.length-a.length);
  const pattern=new RegExp(keys.map(s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'g');
  for(const name of ['index.html','moomoo.html']){
    const html=fs.readFileSync(path.join(root,'assets',name),'utf8');
    assert.match(html,/id="language-select"/);assert.match(html,/\/i18n.js/);
    const texts=[...html.matchAll(/>([^<>]+)</g)].map(m=>m[1].trim());
    for(const source of texts){
      if(!/[\u4e00-\u9fff]/.test(source)||source==='简体中文')continue;
      const translated=en[source]??source.replace(pattern,k=>en[k]);
      assert.ok(!/[\u4e00-\u9fff]/.test(translated),'Missing translation: '+source);
    }
  }
});
