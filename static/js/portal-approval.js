// Keep approval on the current verification screen; trust saved server state only.
(() => {
  if (window.portalApprovalBound) return;
  window.portalApprovalBound = true;
  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('[data-portal-approval]');
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;
    const panel = form.closest('#pending-registration');
    const error = panel.querySelector('[data-portal-approval-error]');
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form),
        headers: {Accept: 'application/json'}, credentials: 'same-origin'
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || 'Approval failed. Please retry.');
      const template = document.createElement('template');
      template.innerHTML = result.html;
      const savedPanel = template.content.querySelector('#pending-registration');
      if (!savedPanel) throw new Error('Could not confirm approval. Refresh to check the saved status.');
      panel.replaceWith(savedPanel);
    } catch (exception) {
      error.textContent = exception.message || 'Could not confirm approval. Please retry.';
      error.hidden = false;
      button.disabled = false;
    }
  });
})();
