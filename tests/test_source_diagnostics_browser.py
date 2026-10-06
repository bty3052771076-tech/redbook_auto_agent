import json
from playwright.sync_api import expect
from test_progress_browser import Scenario, browser, prepare


class SourcesScenario(Scenario):
    def __init__(self):
        super().__init__()
        self.started_check = False
        self.finished = False
        self.row = dict(collection='ai_digest', source_name='official-feed', vendor='模型厂商',
                        source_url='https://example.com/news', status='stale', status_label='可达但无近期消息',
                        connection_status='reachable', action='保留此来源等待新公告。', item_count=10, dated_count=10,
                        recent_count=0, elapsed_seconds=.8, checked_at='2026-10-06T01:00:00Z',
                        latest_published_at='2026-10-01T00:00:00Z', error='')
    def route(self, route):
        path = route.request.url.split('8786', 1)[1]
        if path.startswith('/api/sources'):
            self.calls.append((route.request.method, path))
            if path.endswith('/check'):
                assert route.request.post_data_json == {'collection': 'all', 'keywords': '国际冲突 科技产业 社会民生 财经产业', 'max_age_days': 2}
                assert route.request.headers['idempotency-key']
                self.started_check = True
                data = {'id': '1' * 32, 'status': 'queued'}
            else:
                row = dict(self.row)
                if self.finished:
                    row.update(status='rate_limited', status_label='接口限流', connection_status='failed',
                               error='HTTP 429', action='等待限流恢复或使用官方订阅流。')
                data = {'rows': [row], 'check': {'id': '1'*32, 'status': 'completed' if self.finished else 'running',
                                                'stage': '检查信源', 'message': '检测完成' if self.finished else '1/87 正在检测'} if self.started_check else None}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data, ensure_ascii=False))
            return
        super().route(route)


def test_source_diagnostics_click_polling_filters_and_responsive_layout(browser, tmp_path):
    scenario = SourcesScenario()
    context, page, errors = prepare(browser, scenario)
    page.get_by_role('button', name='信源健康', exact=True).click()
    expect(page.get_by_role('heading', name='信源健康', exact=True)).to_be_visible()
    expect(page.get_by_text('可达但无近期消息', exact=True)).to_be_visible()
    page.get_by_role('button', name='检查信源', exact=True).click()
    expect(page.get_by_role('button', name='检测中', exact=True)).to_be_disabled()
    expect(page.get_by_role('status')).to_contain_text('1/87')
    assert scenario.calls.count(('POST', '/api/sources/check')) == 1
    scenario.finished = True
    expect(page.get_by_text('接口限流', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='检查信源', exact=True)).to_be_enabled()
    page.get_by_label('仅请求失败', exact=True).check()
    expect(page.get_by_text('HTTP 429', exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / 'sources-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_function('() => document.querySelector(".sidebar").getBoundingClientRect().right <= 0')
    page.screenshot(path=str(tmp_path / 'sources-mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not any('/confirm' in path for _, path in scenario.calls)
    assert not errors
    context.close()
