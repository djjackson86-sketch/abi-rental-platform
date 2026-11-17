(() => {
  'use strict';
  const root = document.querySelector('.monthly-revenue');
  if (!root) return;
  const months = JSON.parse(document.getElementById('monthly-revenue-data').textContent);
  const svg = root.querySelector('.monthly-revenue-chart');
  const checks = [...root.querySelectorAll('.monthly-revenue-months input')];
  const colors = ['#2563eb','#b45309','#15803d','#b91c1c','#7e22ce','#0e7490','#be185d','#4d7c0f','#4338ca','#a16207','#0f766e','#9f1239'];
  const dashes = ['', '8 4', '3 4', '12 4 3 4'];
  const symbol = document.currentScript?.dataset.currencySymbol || 'R';
  const revenueLabel = document.currentScript?.dataset.revenueLabel || 'Gross Revenue';
  svg.setAttribute('aria-label', revenueLabel + ' received in rand, days 1 to 31');
  const rand = value => (value < 0 ? '-' : '') + symbol + Math.abs(value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}).replace(/,/g, ' ');
  function element(tag, attrs, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    svg.appendChild(node);
    return node;
  }
  checks.forEach((check, i) => {
    const sample = check.parentElement.querySelector('line');
    sample.setAttribute('stroke', colors[i]);
    sample.setAttribute('stroke-dasharray', dashes[i % 4]);
    check.addEventListener('change', draw);
  });
  function draw() {
    svg.replaceChildren();
    const selected = months.filter((month, i) => checks[i].checked);
    root.querySelector('.monthly-revenue-status').textContent = selected.length ?
      revenueLabel + ': ' + selected.map(m => m.label).join(' · ') + '. Hover a point for its value. Scroll sideways on small screens to see all 31 days.' :
      'Select at least one month to show revenue.';
    const values = selected.flatMap(m => m.values.filter(v => v !== null));
    let low = Math.min(0, ...values), high = Math.max(0, ...values);
    if (high === low) high = low + 100;
    const step = Math.pow(10, Math.floor(Math.log10((high - low) / 5)));
    const tick = Math.ceil((high - low) / 5 / step) * step;
    low = Math.floor(low / tick) * tick;
    high = Math.ceil(high / tick) * tick;
    const x = day => 105 + (day - 1) * 30;
    const y = value => 315 - (value - low) / (high - low) * 280;
    for (let value = low; value <= high + tick / 100; value += tick) {
      element('line', {x1:105, y1:y(value), x2:1005, y2:y(value), stroke: value === 0 ? '#64748b' : '#e2e8f0'});
      element('text', {x:96, y:y(value)+4, 'text-anchor':'end', fill:'#475569', 'font-size':12}, rand(value));
    }
    for (let day = 1; day <= 31; day++) {
      element('text', {x:x(day), y:338, 'text-anchor':'middle', fill:'#475569', 'font-size':12}, day);
    }
    element('text', {x:555, y:367, 'text-anchor':'middle', fill:'#334155', 'font-size':14}, 'Day of month');
    element('text', {x:20, y:175, transform:'rotate(-90 20 175)', 'text-anchor':'middle', fill:'#334155', 'font-size':14}, 'Revenue (' + symbol + ')' );
    selected.forEach(month => {
      const i = month.month - 1;
      const points = month.values.map((value, day) => value === null ? null : `${x(day+1)},${y(value)}`).filter(v => v !== null).join(' ');
      element('polyline', {points, fill:'none', stroke:colors[i], 'stroke-width':2.5, 'stroke-dasharray':dashes[i % 4], 'data-month':month.month});
      month.values.forEach((value, day) => {
        if (value === null) return;
        const point = element('circle', {cx:x(day+1), cy:y(value), r:3, fill:colors[i]});
        const title = document.createElementNS(svg.namespaceURI, 'title');
        title.textContent = `${revenueLabel} · ${month.label} ${day+1}: ${rand(value)}`;
        point.appendChild(title);
      });
    });
    const table = root.querySelector('.monthly-revenue-table');
    table.replaceChildren();
    const header = table.createTHead().insertRow();
    ['Day', ...selected.map(m => m.label)].forEach(label => {
      const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; header.appendChild(cell);
    });
    const body = table.createTBody();
    for (let day = 0; day < 31; day++) {
      const row = body.insertRow();
      const heading = document.createElement('th'); heading.scope = 'row'; heading.textContent = day + 1; row.appendChild(heading);
      selected.forEach(m => { row.insertCell().textContent = m.values[day] === null ? '—' : rand(m.values[day]); });
    }
  }
  draw();
})();
