from pathlib import Path


def test_pending_queue_is_inside_right_hand_customer_workspace():
    template = (Path(__file__).resolve().parents[1] / 'templates/admin/customers/index.html').read_text(encoding='utf-8')
    queue = '{% include "admin/customers/_pending_registrations_queue.html" %}'
    assert template.count(queue) == 1
    rail_end = template.index('</aside>')
    workspace = template.index('<main class="customers-workspace">')
    metrics = template.index('<section class="metrics customers-metrics">')
    assert rail_end < workspace < template.index(queue) < metrics < template.index('</main>')
