(function() {
  function formatNumber(n) {
    if (n === null || n === undefined) return '-';
    return Number(n).toLocaleString('zh-CN');
  }

  function formatDate(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  function showLoading(selector) {
    var el = document.querySelector(selector);
    if (el) el.classList.add('loading-skeleton');
  }

  function hideLoading(selector) {
    var el = document.querySelector(selector);
    if (el) el.classList.remove('loading-skeleton');
  }

  function setError(selector, message) {
    var el = document.querySelector(selector);
    if (el) {
      el.classList.remove('loading-skeleton');
      el.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">' + (message || '加载失败') + '</div></div>';
    }
  }

  function fetchSection(url, selector, onSuccess) {
    showLoading(selector);
    fetch(url)
      .then(function(res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function(data) {
        hideLoading(selector);
        onSuccess(data);
      })
      .catch(function(err) {
        console.error('Dashboard section failed:', url, err);
        setError(selector, '加载失败，请刷新重试');
      });
  }

  function renderOverview(data) {
    var cards = document.getElementById('overviewCards');
    if (!cards) return;
    var values = {
      total_chats: data.total_chats,
      active_chats: data.active_chats,
      total_messages: data.total_messages,
      messages_24h: data.messages_24h,
      total_users: data.total_users,
    };
    cards.querySelectorAll('[data-metric]').forEach(function(card) {
      var key = card.dataset.metric;
      if (values.hasOwnProperty(key)) {
        card.querySelector('.value').innerHTML = values[key];
      }
    });
    // delta
    var delta = cards.querySelector('[data-delta]');
    if (delta && data.messages_7d > 0) {
      var avg = data.messages_7d / 7;
      var up = data.messages_24h >= avg;
      delta.innerHTML = (up ? '▲' : '▼') + ' vs 7日平均 ' + Math.round(avg);
      delta.className = 'delta ' + (up ? 'up' : 'down');
    }
  }

  function renderActivity(data) {
    if (data.daily_rows && data.daily_rows.length) {
      var ctx = document.getElementById('dailyChart');
      if (ctx && typeof Chart !== 'undefined') {
        new Chart(ctx.getContext('2d'), {
          type: 'bar',
          data: {
            labels: data.daily_rows.map(function(r) { return r.day.slice(5); }),
            datasets: [{
              data: data.daily_rows.map(function(r) { return r.count; }),
              backgroundColor: 'rgba(99, 102, 241, 0.75)',
              hoverBackgroundColor: 'rgba(99, 102, 241, 0.95)',
              borderRadius: 8,
              borderSkipped: false,
            }]
          },
          options: window.makeChartOptions()
        });
      }
    } else {
      var wrap = document.getElementById('dailyChartPanel');
      if (wrap) wrap.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">暂无数据</div></div>';
    }

    if (data.hourly_dist && data.hourly_dist.length) {
      var ctx2 = document.getElementById('hourlyChart');
      if (ctx2 && typeof Chart !== 'undefined') {
        new Chart(ctx2.getContext('2d'), {
          type: 'bar',
          data: {
            labels: data.hourly_dist.map(function(r) { return r.hour + ':00'; }),
            datasets: [{
              data: data.hourly_dist.map(function(r) { return r.count; }),
              backgroundColor: 'rgba(16, 185, 129, 0.75)',
              hoverBackgroundColor: 'rgba(16, 185, 129, 0.95)',
              borderRadius: 6,
              borderSkipped: false,
            }]
          },
          options: window.makeChartOptions()
        });
      }
    } else {
      var wrap2 = document.getElementById('hourlyChartPanel');
      if (wrap2) wrap2.innerHTML = '<div class="empty-state"><div class="empty-icon">🕐</div><div class="empty-title">暂无数据</div></div>';
    }
  }

  function renderTopChats(data) {
    var tbody = document.querySelector('#topChatsTable tbody');
    if (!tbody) return;
    if (!data.top_chats || !data.top_chats.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">暂无数据</td></tr>';
      return;
    }
    tbody.innerHTML = data.top_chats.map(function(item, idx) {
      return '<tr><td><span class="badge badge-blue">' + (idx + 1) + '</span></td>' +
        '<td><a href="/chats/' + item.chat_id + '" style="color:var(--text-primary);text-decoration:none;font-weight:600;">' + escapeHtml(item.title) + '</a></td>' +
        '<td style="font-variant-numeric:tabular-nums;">' + formatNumber(item.message_count) + '</td></tr>';
    }).join('');
  }

  function renderTopSenders(data) {
    var tbody = document.querySelector('#topSendersTable tbody');
    if (!tbody) return;
    if (!data.top_senders || !data.top_senders.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">暂无数据</td></tr>';
      return;
    }
    tbody.innerHTML = data.top_senders.map(function(item, idx) {
      return '<tr><td><span class="badge badge-blue">' + (idx + 1) + '</span></td>' +
        '<td>' + escapeHtml(item.sender) + '</td>' +
        '<td style="font-variant-numeric:tabular-nums;">' + formatNumber(item.message_count) + '</td></tr>';
    }).join('');
  }

  function renderTopKeywords(data) {
    var wrap = document.getElementById('topKeywordsList');
    if (!wrap) return;
    if (!data.top_keywords || !data.top_keywords.length) {
      wrap.innerHTML = '<div class="empty-state"><div class="empty-icon">🔑</div><div class="empty-title">暂无数据</div></div>';
      return;
    }
    wrap.innerHTML = data.top_keywords.map(function(item) {
      return '<span>' + escapeHtml(item.keyword) + ' <span class="weight">' + formatNumber(item.weight) + '</span></span>';
    }).join('');
  }

  function renderAiStats(data) {
    var cards = document.getElementById('overviewCards');
    if (!cards) return;
    var values = {
      ai_summaries: (data.success_summaries || 0) + '<span style="font-size:13px;color:var(--text-muted);"> / ' + (data.total_summaries || 0) + '</span>',
      total_urls: data.total_urls,
      total_products: data.total_products,
      total_contacts: data.total_contacts,
      total_alert_rules: data.total_alert_rules,
      unread_alerts: data.unread_alerts,
    };
    cards.querySelectorAll('[data-metric]').forEach(function(card) {
      var key = card.dataset.metric;
      if (values.hasOwnProperty(key)) {
        card.querySelector('.value').innerHTML = values[key];
      }
    });
  }

  function renderSyncStatus(data) {
    var tbody = document.querySelector('#recentSyncsTable tbody');
    if (!tbody) return;
    var runs = data.recent_runs || [];
    if (!runs.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无同步记录</td></tr>';
      return;
    }
    var statusMap = {
      success: '<span class="badge badge-green">成功</span>',
      running: '<span class="badge badge-amber">运行中</span>',
      failed: '<span class="badge badge-red">失败</span>',
    };
    tbody.innerHTML = runs.map(function(r) {
      return '<tr><td><span class="badge badge-blue">' + escapeHtml(r.run_type || 'sync') + '</span></td>' +
        '<td>' + (statusMap[r.status] || '<span class="badge badge-blue">' + escapeHtml(r.status) + '</span>') + '</td>' +
        '<td>' + escapeHtml(r.message || '') + '</td>' +
        '<td style="white-space:nowrap;font-size:12px;color:var(--text-muted);">' + formatDate(r.started_at) + '</td></tr>';
    }).join('');
  }

  function escapeHtml(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  document.addEventListener('DOMContentLoaded', function() {
    fetchSection('/api/dashboard/overview', '#overviewCards', renderOverview);
    fetchSection('/api/dashboard/activity', '#activitySection', renderActivity);
    fetchSection('/api/dashboard/top-chats', '#topChatsSection', renderTopChats);
    fetchSection('/api/dashboard/top-senders', '#topSendersSection', renderTopSenders);
    fetchSection('/api/dashboard/top-keywords', '#topKeywordsSection', renderTopKeywords);
    fetchSection('/api/dashboard/ai-stats', '#overviewCards', renderAiStats);
    fetchSection('/api/dashboard/sync-status', '#syncStatusSection', renderSyncStatus);
  });
})();
