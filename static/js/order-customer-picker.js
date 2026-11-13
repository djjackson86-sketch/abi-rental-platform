/*
 * Bounded customer picker for the order form.
 *
 * The page used to carry every client in the book (5 034 on the live install,
 * ~9.3 MB of HTML) inside a <datalist>. It now carries at most the one client
 * already selected, and this file asks the server for the rest:
 *
 *   GET /orders/customer-search?q=...      -> at most 20 identity rows
 *   GET /orders/customer-summary/<id>      -> one full summary, only after a pick
 *
 * Rules that matter for the order form's safety:
 *   * typing over a selection immediately clears the hidden customer_id, so a
 *     half-typed name can never be saved against the previously picked client;
 *   * a search or summary response that arrives after the text (or the
 *     selection) has moved on is discarded, and each fetch aborts its predecessor;
 *   * the native <datalist> is kept for browsers/phones that render its
 *     typeahead, with a visible results list beside it for touch and keyboard use.
 *
 * The form's existing card/banner/stats code stays in form.html; this file only
 * calls window.orderCustomerPicker.refresh()/present().
 */
(function () {
  'use strict';

  var input = document.getElementById('customer-search');
  if (!input) return;
  var datalist = document.getElementById('customer-options');
  var hidden = document.getElementById('customer-id');
  var results = document.getElementById('customer-picker-results');
  var feedback = document.getElementById('customer-search-feedback');
  if (!datalist || !hidden) return;

  var searchUrl = input.getAttribute('data-customer-search-url') || '';
  var summaryUrl = input.getAttribute('data-customer-summary-url') || '';
  var orderId = input.getAttribute('data-customer-order-id') || '';
  var MIN_CHARS = 2;
  var DEBOUNCE_MS = 250;

  var timer = null;
  var searchController = null;
  var summaryController = null;
  var summaryToken = 0;
  var lastResults = [];

  function api() { return window.orderCustomerPicker || {}; }

  function options() {
    return Array.prototype.slice.call(datalist.querySelectorAll('option'));
  }

  function refreshOptions() {
    if (typeof api().refresh === 'function') api().refresh();
  }

  function present(summary) {
    if (typeof api().present === 'function') api().present(summary);
    var banner = document.getElementById('customer-search-feedback');
    if (!banner) return;
    if (!summary) { banner.hidden = true; banner.textContent = ''; return; }
    banner.hidden = false;
    banner.textContent = 'Attached: ' + (summary.display || summary.name || '');
  }

  function optionFor(id) {
    id = String(id);
    return options().filter(function (o) { return String(o.dataset.id) === id; })[0] || null;
  }

  function setHiddenId(id) {
    hidden.value = id ? String(id) : '';
    var select = document.getElementById('customer-select');
    if (select) select.value = hidden.value;
  }

  function setExpanded(open) {
    input.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function clearResults() {
    lastResults = [];
    if (results) {
      results.hidden = true;
      results.innerHTML = '';
    }
    setExpanded(false);
  }

  function summaryUrlFor(id) {
    var url = summaryUrl.replace(/0$/, encodeURIComponent(String(id)));
    if (orderId) url += '?order_id=' + encodeURIComponent(orderId);
    return url;
  }

  function fetchSummary(id) {
    var token = ++summaryToken;
    if (summaryController) summaryController.abort();
    summaryController = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    return fetch(summaryUrlFor(id), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
      signal: summaryController ? summaryController.signal : undefined
    }).then(function (response) {
      if (!response.ok) throw new Error('summary');
      return response.json();
    }).then(function (data) {
      // Stale or superseded: another client was picked since this request went out.
      if (token !== summaryToken) return null;
      if (!data || !data.customer || String(data.customer.id) !== String(id)) return null;
      if (hidden.value !== String(id)) return null;
      return data.customer;
    });
  }

  function attach(id, summary) {
    setHiddenId(id);
    if (summary) { present(summary); return; }
    present(null);
    fetchSummary(id).then(function (customer) {
      if (customer) {
        // Persist the fetched summary for the legacy change/blur listener too.
        // Otherwise tabbing away immediately clears the freshly filled cards.
        var option = optionFor(id);
        if (!option) {
          option = document.createElement('option');
          option.dataset.id = String(id);
          option.value = customer.display || customer.name || '';
          datalist.appendChild(option);
        }
        option.dataset.summary = JSON.stringify(customer);
        refreshOptions();
        present(customer);
      }
    }).catch(function () { /* keep the id; the card fills when re-picked */ });
  }

  function chooseFromOption(option) {
    if (!option) return;
    var summary = null;
    if (option.dataset.summary) {
      try { summary = JSON.parse(option.dataset.summary); } catch (err) { summary = null; }
    }
    attach(option.dataset.id, summary);
  }

  function renderResults(customers) {
    lastResults = customers || [];
    var selected = optionFor(hidden.value);
    datalist.innerHTML = '';
    lastResults.forEach(function (customer) {
      var option = document.createElement('option');
      option.value = customer.display || customer.name || '';
      option.label = customer.email || customer.name || '';
      option.dataset.id = String(customer.id);
      datalist.appendChild(option);
    });
    // Never drop the current selection's preloaded summary while a search result
    // set is replacing the datalist.
    if (selected && selected.dataset.summary && !optionFor(selected.dataset.id)) {
      datalist.appendChild(selected.cloneNode(true));
    }
    refreshOptions();

    if (!results) return;
    results.innerHTML = '';
    if (!lastResults.length) {
      results.hidden = false;
      var empty = document.createElement('p');
      empty.className = 'form-help';
      empty.textContent = 'No client matches that search.';
      results.appendChild(empty);
      setExpanded(true);
      return;
    }
    lastResults.forEach(function (customer) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'customer-picker-option';
      button.setAttribute('role', 'option');
      button.setAttribute('data-customer-id', String(customer.id));
      button.innerHTML = '';
      var name = document.createElement('strong');
      name.textContent = customer.name || customer.display || '';
      var email = document.createElement('span');
      email.textContent = customer.email || '';
      button.appendChild(name);
      if (customer.email) button.appendChild(email);
      button.addEventListener('click', function () {
        input.value = customer.display || customer.name || '';
        attach(customer.id, null);
        clearResults();
        input.focus();
      });
      results.appendChild(button);
    });
    results.hidden = false;
    setExpanded(true);
  }

  function search() {
    clearTimeout(timer);
    var query = input.value.trim();
    if (!options().some(function (o) { return o.value === input.value; })) {
      // Any text edit that no longer exactly names a known client detaches it.
      if (hidden.value) { setHiddenId(''); present(null); }
      clearResults();
    }
    if (query.length < MIN_CHARS) {
      if (searchController) searchController.abort();
      return;
    }
    if (searchController) searchController.abort();
    searchController = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    clearTimeout(timer);
    timer = setTimeout(function () {
      fetch(searchUrl + '?q=' + encodeURIComponent(query.slice(0, 80)), {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' },
        signal: searchController ? searchController.signal : undefined
      }).then(function (response) {
        if (!response.ok) throw new Error('search');
        return response.json();
      }).then(function (data) {
        // Text moved on while this response was in flight — discard it.
        if (input.value.trim() !== query) return;
        renderResults((data && data.customers) || []);
      }).catch(function () { /* aborted or failed: keep whatever is selected */ });
    }, DEBOUNCE_MS);
  }

  input.addEventListener('input', search);
  input.addEventListener('change', function () {
    var option = options().filter(function (o) { return o.value === input.value; })[0] || null;
    if (option) {
      chooseFromOption(option);
      clearResults();
    }
  });
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { clearResults(); }
  });
  setExpanded(false);
})();
