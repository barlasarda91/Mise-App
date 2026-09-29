// Convenience-state persistence across form-submit reloads.
//
// Every action in Mise is a plain form POST + redirect, so the page reloads
// constantly. Two kinds of harmless-but-annoying state loss are handled here:
//   1. <details> elements (expanded thread messages, checklist groups)
//      collapse on reload — remember open ones per page and re-open them.
//   2. Text typed into one form is wiped when a *different* form on the same
//      page submits — snapshot user-modified fields at submit time and
//      restore them once after the reload. Only fields whose name is unique
//      on the page are saved, so a value can never land in the wrong slot,
//      and a field the server re-rendered with new content is never
//      overwritten (restore only applies to fields still at their default).
(function () {
  const PAGE = location.pathname + location.search;
  const TTL_MS = 15 * 60 * 1000;

  function load(key) {
    try {
      const raw = JSON.parse(sessionStorage.getItem(key));
      if (raw && raw.page === PAGE && Date.now() - raw.ts < TTL_MS) return raw;
    } catch (e) {}
    return null;
  }
  function save(key, data) {
    try { sessionStorage.setItem(key, JSON.stringify(Object.assign({ page: PAGE, ts: Date.now() }, data))); } catch (e) {}
  }

  // ---- 1. open <details> survive reloads ----
  const DKEY = 'mise-open-details';
  const detailsEls = Array.from(document.querySelectorAll('details'));
  const dkey = function (el, ix) { return el.dataset.key || 'd' + ix; };
  const saved = load(DKEY);
  const openSet = saved ? saved.open.slice() : [];
  detailsEls.forEach(function (el, ix) {
    if (openSet.indexOf(dkey(el, ix)) !== -1) el.open = true;
    el.addEventListener('toggle', function () {
      const k = dkey(el, ix);
      const at = openSet.indexOf(k);
      if (el.open && at === -1) openSet.push(k);
      if (!el.open && at !== -1) openSet.splice(at, 1);
      save(DKEY, { open: openSet });
    });
  });

  // ---- instruction boxes: Enter = newline, Shift+Enter = submit ----
  document.querySelectorAll('textarea.submit-on-shift-enter').forEach(function (el) {
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && ev.shiftKey) {
        ev.preventDefault();
        if (!el.form) return;
        if (el.form.requestSubmit) el.form.requestSubmit();
        else el.form.submit();
      }
    });
  });

  // ---- contact autocomplete on To / Cc / Bcc fields ----
  // Suggests from /contacts.json (mail-index correspondents + lead contacts),
  // completing only the segment after the last comma so multi-recipient
  // fields stay editable. Arrow keys + Enter or click to pick; Esc closes.
  (function () {
    var fields = Array.from(document.querySelectorAll(
      'input[name="to"]:not([type=hidden]), input[name="cc"]:not([type=hidden]), input[name="bcc"]:not([type=hidden])'
    ));
    if (!fields.length) return;
    var contacts = null, fetching = false;
    var box = document.createElement('div');
    box.hidden = true;
    box.style.cssText = 'position:absolute;z-index:40;min-width:260px;max-width:420px;' +
      'background:var(--paper,#fff);border:1px solid var(--line,#999);box-shadow:0 4px 14px rgba(0,0,0,.12);' +
      'font-size:13px;max-height:240px;overflow-y:auto';
    document.body.appendChild(box);
    var active = null, items = [], sel = -1;

    function hide() { box.hidden = true; items = []; sel = -1; }
    function segment(value) {
      var i = value.lastIndexOf(',');
      return { head: i >= 0 ? value.slice(0, i + 1) : '', tail: value.slice(i + 1).trim() };
    }
    function pick(ix) {
      if (!active || !items[ix]) return;
      var seg = segment(active.value);
      active.value = seg.head + (seg.head ? ' ' : '') + items[ix].email + ', ';
      hide();
      active.focus();
    }
    function paint() {
      box.innerHTML = '';
      items.forEach(function (c, ix) {
        var row = document.createElement('div');
        row.style.cssText = 'padding:6px 10px;cursor:pointer;display:flex;gap:8px;align-items:baseline' +
          (ix === sel ? ';background:var(--panel,#eee)' : '');
        var name = document.createElement('span');
        name.textContent = c.name || c.email;
        name.style.cssText = 'font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
        var mail = document.createElement('span');
        mail.textContent = c.name ? c.email : (c.hint || '');
        mail.style.cssText = 'color:var(--ink-soft,#777);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
        row.appendChild(name); row.appendChild(mail);
        if (c.hint && c.name) {
          var hint = document.createElement('span');
          hint.textContent = '· ' + c.hint;
          hint.style.cssText = 'color:var(--ink-soft,#777);font-size:12px;white-space:nowrap';
          row.appendChild(hint);
        }
        row.addEventListener('mousedown', function (ev) { ev.preventDefault(); pick(ix); });
        box.appendChild(row);
      });
    }
    function render(input) {
      var q = segment(input.value).tail.toLowerCase();
      if (q.length < 2 || !contacts) return hide();
      var already = input.value.toLowerCase();
      items = contacts.filter(function (c) {
        if (already.indexOf(c.email) !== -1) return false;
        return c.email.indexOf(q) !== -1 ||
          (c.name && c.name.toLowerCase().indexOf(q) !== -1) ||
          (c.hint && c.hint.toLowerCase().indexOf(q) !== -1);
      }).slice(0, 8);
      if (!items.length) return hide();
      sel = -1; active = input; paint();
      var r = input.getBoundingClientRect();
      box.style.left = (r.left + window.scrollX) + 'px';
      box.style.top = (r.bottom + window.scrollY + 2) + 'px';
      box.style.minWidth = Math.min(r.width, 420) + 'px';
      box.hidden = false;
    }
    function ensureLoaded(then) {
      if (contacts) return then();
      if (fetching) return;
      fetching = true;
      fetch('/contacts.json').then(function (r) { return r.json(); })
        .then(function (d) { contacts = Array.isArray(d) ? d : []; then(); })
        .catch(function () { contacts = []; });
    }
    fields.forEach(function (input) {
      input.setAttribute('autocomplete', 'off');
      input.addEventListener('input', function () {
        ensureLoaded(function () { render(input); });
        if (contacts) render(input);
      });
      input.addEventListener('keydown', function (ev) {
        if (box.hidden) return;
        if (ev.key === 'ArrowDown') { ev.preventDefault(); sel = Math.min(sel + 1, items.length - 1); paint(); }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); sel = Math.max(sel - 1, 0); paint(); }
        else if (ev.key === 'Enter' && sel >= 0) { ev.preventDefault(); pick(sel); }
        else if (ev.key === 'Escape') { hide(); }
      });
      input.addEventListener('blur', function () { setTimeout(hide, 120); });
    });
  })();

  // ---- select-all master checkboxes (data-check-all="<name>") ----
  document.addEventListener('change', function (ev) {
    const master = ev.target;
    if (!master.matches || !master.matches('input[type=checkbox][data-check-all]')) return;
    const formId = master.getAttribute('form');
    let sel = 'input[type=checkbox][name="' + master.dataset.checkAll + '"]';
    if (formId) sel += '[form="' + formId + '"]';
    document.querySelectorAll(sel).forEach(function (cb) { cb.checked = master.checked; });
  });

  // ---- 2. typed fields survive another form's submit ----
  const FKEY = 'mise-typed-fields';
  const SELECTOR = 'input[type=text], input[type=email], input[type=date], input:not([type]), textarea, select';

  function editable(form) {
    return Array.from(form.querySelectorAll(SELECTOR));
  }
  function fieldDefault(el) {
    if (el.tagName === 'SELECT') {
      const def = Array.from(el.options).find(function (o) { return o.defaultSelected; });
      return def ? def.value : (el.options[0] ? el.options[0].value : '');
    }
    return el.defaultValue;
  }
  function uniqueNames() {
    const counts = {};
    document.querySelectorAll(SELECTOR).forEach(function (el) {
      if (el.name) counts[el.name] = (counts[el.name] || 0) + 1;
    });
    return counts;
  }

  document.addEventListener('submit', function (ev) {
    const submitted = ev.target;
    const counts = uniqueNames();
    const fields = {};
    document.querySelectorAll('form').forEach(function (form) {
      if (form === submitted) return;
      editable(form).forEach(function (el) {
        if (!el.name || counts[el.name] !== 1) return;
        if (el.value !== fieldDefault(el)) fields[el.name] = el.value;
      });
    });
    if (Object.keys(fields).length) save(FKEY, { fields: fields });
    else try { sessionStorage.removeItem(FKEY); } catch (e) {}
  }, true);

  const stash = load(FKEY);
  if (stash) {
    try { sessionStorage.removeItem(FKEY); } catch (e) {}
    const counts = uniqueNames();
    Object.keys(stash.fields).forEach(function (name) {
      if (counts[name] !== 1) return;
      const el = Array.from(document.querySelectorAll(SELECTOR)).find(function (e) { return e.name === name; });
      if (el && el.value === fieldDefault(el) && el.value !== stash.fields[name]) {
        el.value = stash.fields[name];
      }
    });
  }
})();
