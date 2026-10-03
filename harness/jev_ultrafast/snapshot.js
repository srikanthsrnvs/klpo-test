(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set(),top=true) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen,false) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || (top ? fallbackName(e) : '');
  };
  // Human-designed pages are full of icon links and div buttons with no accessible name. Derive one the way a
  // person would: from where the link goes, the image it shows, or the words in its class/id.
  const humanize = s => (s||'').replace(/[-_]+/g,' ').replace(/([a-z])([A-Z])/g,'$1 $2').replace(/\b(js|is|has|ui|btn|site|header|wrapper|container|icon|svg|img|active)\b/gi,' ').replace(/\s+/g,' ').trim().toLowerCase();
  const fallbackName = e => {
    const img = e.querySelector && e.querySelector('img,svg');
    if (img) {
      const alt = img.getAttribute('alt') || img.getAttribute('aria-label') || (img.querySelector && img.querySelector('title')?.textContent);
      if (alt && alt.trim()) return alt.trim();
      const use = img.querySelector && img.querySelector('use');
      const src = img.getAttribute('src') || (use && (use.getAttribute('href') || use.getAttribute('xlink:href'))) || '';
      const stem = humanize(src.split(/[\/#]/).pop().replace(/\.(svg|png|jpe?g|gif|webp)$/i,''));
      if (stem && !/^(svg|img|image|logo|sprite)$/.test(stem)) return stem;
    }
    if (e.tagName === 'A' && e.getAttribute('href')) {
      const h = e.getAttribute('href').replace(/^https?:\/\/[^\/]+/,'').replace(/[?#].*$/,'').replace(/\/+$/,'');
      if (h === '' || h === '/') return 'home';
      return humanize(h.split('/').filter(Boolean).slice(-2).join(' '));
    }
    const cls = humanize([e.id, e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className].filter(Boolean).join(' '));
    return cls.split(' ').filter(w => /menu|toggle|close|open|search|nav|burger|cart|account|login|next|prev|play|filter|sort|expand|collapse|more|back|submit|button/.test(w)).join(' ') || cls.split(' ').slice(0,3).join(' ');
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],[onclick],'+
    '[tabindex]:not([tabindex="-1"]),'+roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    if (e.hasAttribute('onclick') || e.hasAttribute('tabindex') || e.__jevPointer) return 'button';
    return null;
  };
  // Pointer-cursor elements outside any semantic control behave as buttons for a person; offer them too.
  const candidates=[...document.querySelectorAll(selector)];
  const semantic=new Set(candidates);
  const covered=e=>{ for (let p=e; p; p=p.parentElement) if (semantic.has(p)) return true; return false; };
  for (const e of document.querySelectorAll('body div,body span,body li,body svg,body i,body img,body label,body p')) {
    if (semantic.has(e) || covered(e) || e.querySelector(selector)) continue;
    const r=e.getBoundingClientRect();
    if (r.width<8 || r.height<8 || r.width>600 || r.height>300 || r.bottom<0 || r.top>innerHeight) continue;
    if (getComputedStyle(e).cursor!=='pointer') continue;
    if (e.parentElement && getComputedStyle(e.parentElement).cursor==='pointer' && !semantic.has(e.parentElement) &&
        !covered(e.parentElement)) continue;  // let the outermost pointer element represent the control
    e.__jevPointer=true; candidates.push(e); semantic.add(e);
  }
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  for (const e of candidates) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions};
})()
