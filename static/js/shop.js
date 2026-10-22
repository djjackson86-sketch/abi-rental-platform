(() => {
  'use strict';
  const nav = document.querySelector('.nav-toggle');
  if (nav) nav.addEventListener('click', () => {
    const open = nav.getAttribute('aria-expanded') !== 'true';
    nav.setAttribute('aria-expanded', String(open));
    document.querySelector('#primary-nav')?.classList.toggle('open', open);
  });
  const select = document.querySelector('[data-hire-product]');
  const summary = document.querySelector('[data-hire-summary]');
  function update() {
    if (!select || !summary) return;
    const option = select.selectedOptions[0];
    if (!option?.value) {
      summary.textContent = 'Select a trailer to see its rate and deposit.';
      return;
    }
    summary.replaceChildren();
    if (option.dataset.image) {
      const image = document.createElement('img');
      image.src = option.dataset.image;
      image.alt = option.textContent;
      image.style.cssText = 'width:100%;height:200px;object-fit:contain';
      summary.append(image);
    }
    if (option.dataset.rate === '') {
      const note = document.createElement('p');
      note.textContent = 'Your branch will confirm the rental rate and deposit.';
      summary.append(note);
    } else {
      const rate = document.createElement('p');
      rate.textContent = 'Inventory rate: R' + Number(option.dataset.rate || 0).toFixed(2);
      const deposit = document.createElement('p');
      deposit.textContent = 'Refundable deposit: R' + Number(option.dataset.deposit || 0).toFixed(2);
      summary.append(rate, deposit);
    }
    if (option.dataset.description) {
      const description = document.createElement('p');
      description.textContent = option.dataset.description;
      summary.append(description);
    }
  }
  if (select) {
    select.addEventListener('change', update);
    update();
  }
  function reset() {
    document.querySelectorAll('[data-send-request]').forEach(button => {
      button.disabled = false;
      button.textContent = button.dataset.originalText || button.textContent;
    });
  }
  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', () => {
    const button = form.querySelector('[data-send-request]');
    if (button) {
      button.dataset.originalText = button.textContent;
      button.disabled = true;
      button.textContent = 'Sending enquiry…';
    }
  }));
  window.addEventListener('pageshow', reset);
  window.addEventListener('load', reset);
})();
