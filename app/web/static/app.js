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
