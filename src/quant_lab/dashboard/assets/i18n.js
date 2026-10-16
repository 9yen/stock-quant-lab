/* Source-string catalogs. Runtime-only localization; never changes trading state. */
(() => {
  'use strict';
  const supported=['zh-CN','en'];
  let locale='zh-CN', catalog={}, matcher=null, templates=[], revision=0;
  const textSources=new WeakMap(), attributeSources=new WeakMap();
  const attrs=['aria-label','placeholder','title'];
  const escaped=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  function translate(value){
    const source=String(value??'');
    if(locale==='zh-CN'||!matcher)return source;
    const key=source.trim();
    if(Object.hasOwn(catalog,key))return source.replace(key,catalog[key]);
    for(const template of templates){
      const match=key.match(template.pattern);
      if(match){
        const rendered=template.output.replace(/\{(\d+)\}/g,(_,index)=>match[template.indices.indexOf(index)+1]??'');
        return source.replace(key,rendered);
      }
    }
    const rendered=source.replace(matcher,key=>catalog[key]);
    // New, unknown broker messages retain their source rather than becoming mixed fragments.
    return /[\u4e00-\u9fff]/.test(rendered)?source:rendered;
  }
  function translateText(node){
    if(!node.parentElement||node.parentElement.closest('script,style,textarea,[data-no-i18n]'))return;
    const current=node.nodeValue, saved=textSources.get(node);
    const source=saved&&current===saved.rendered?saved.source:current;
    const rendered=translate(source);
    textSources.set(node,{source,rendered});
    if(current!==rendered)node.nodeValue=rendered;
  }
  function translateElement(element){
    if(element.closest('script,style,textarea,[data-no-i18n]'))return;
    let saved=attributeSources.get(element);
    if(!saved){saved={};attributeSources.set(element,saved);}
    for(const name of attrs){
      const current=element.getAttribute(name);if(current===null)continue;
      const previous=saved[name],source=previous&&current===previous.rendered?previous.source:current;
      const rendered=translate(source);saved[name]={source,rendered};
      if(current!==rendered)element.setAttribute(name,rendered);
    }
  }
  function walk(root){
    if(root.nodeType===Node.TEXT_NODE){translateText(root);return;}
    if(root.nodeType!==Node.ELEMENT_NODE)return;
    translateElement(root);
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT);
    for(let node=walker.nextNode();node;node=walker.nextNode()){
      if(node.nodeType===Node.TEXT_NODE)translateText(node);else translateElement(node);
    }
  }
  const observation={subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:attrs};
  const observer=new MutationObserver(records=>{
    observer.disconnect();
    for(const record of records){
      if(record.type==='childList')for(const node of record.addedNodes)walk(node);
      else walk(record.target);
    }
    observer.observe(document.body,observation);
  });
  async function setLocale(requested){
    if(!supported.includes(requested))return false;
    const currentRevision=++revision;
    let loaded;
    try{
      const response=await fetch('/locales/'+requested+'.json',{cache:'force-cache'});
      if(!response.ok)throw Error('Locale unavailable');
      loaded=await response.json();
      if(!loaded||Array.isArray(loaded)||Object.values(loaded).some(v=>typeof v!=='string'))throw Error('Invalid catalog');
    }catch{return false;}
    if(currentRevision!==revision)return false;
    locale=requested;catalog=loaded;
    templates=Object.keys(catalog).filter(key=>/\{\d+\}/.test(key)).map(key=>{
      const indices=[];
      const pattern=key.split(/(\{\d+\})/g).map(part=>{
        if(/^\{\d+\}$/.test(part)){indices.push(part.slice(1,-1));return '(.*?)';}
        return escaped(part);
      }).join('');
      return {pattern:new RegExp('^'+pattern+'$','s'),indices,output:catalog[key]};
    });
    matcher=new RegExp(Object.keys(catalog).sort((a,b)=>b.length-a.length).map(escaped).join('|'),'g');
    observer.disconnect();document.documentElement.lang=locale;walk(document.body);walk(document.querySelector('title'));
    document.getElementById('language-select').value=locale;
    observer.observe(document.body,observation);
    try{localStorage.setItem('quant-lab-locale',locale);}catch{}
    window.dispatchEvent(new Event('quant:locale'));
    return true;
  }
  let initial=(navigator.language||'en').toLowerCase().startsWith('zh')?'zh-CN':'en';
  try{const saved=localStorage.getItem('quant-lab-locale');if(supported.includes(saved))initial=saved;}catch{}
  window.QuantI18n={t:translate,setLocale,get locale(){return locale;}};
  document.getElementById('language-select').addEventListener('change',event=>{setLocale(event.target.value);});
  observer.observe(document.body,observation);
  setLocale(initial);
})();
