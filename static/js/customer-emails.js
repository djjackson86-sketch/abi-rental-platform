// ABI-341953240: the main email does not count towards the three-copy limit.
(() => {
  function validate(field) {
    const form = field.form;
    const company = form.querySelector('input[name="customer_type"]:checked');
    if (field.disabled || field.closest('label').hidden || (company && company.value !== 'company')) {
      field.setCustomValidity('');
      return;
    }
    const primary = (form.querySelector('input[name="email"]')?.value || '').trim().toLowerCase();
    const addresses = [...new Set(field.value.split(';').map(s => s.trim().toLowerCase()).filter(s => s && s !== primary))];
    let message = '';
    if (addresses.length > 3) message = 'Enter a maximum of 3 other email addresses, excluding the main email.';
    else if (/[\x00-\x1f\x7f\u0085\u2028\u2029]/.test(field.value) || addresses.some(s => !/^[^\s@<>(),;]+@[^\s@<>(),;]+$/.test(s))) message = 'Enter valid email addresses separated by semicolons (;).';
    field.setCustomValidity(message);
  }
  function validateForm(form) {
    form?.querySelectorAll('textarea[name="other_email_addresses"]').forEach(validate);
  }
  document.querySelectorAll('textarea[name="other_email_addresses"]').forEach(validate);
  ['input', 'change'].forEach(event => document.addEventListener(event, e => validateForm(e.target.form)));
  // Recheck just before native validation, including forms submitted by requestSubmit().
  document.addEventListener('click', e => {
    const button = e.target.closest('button, input[type="submit"]');
    if (button) validateForm(button.form);
  }, true);
})();
