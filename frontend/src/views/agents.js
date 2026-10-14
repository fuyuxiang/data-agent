/**
 * 智能体（用户端）：业务用户挑选智能体的入口。
 *
 * 不展示模型、技能清单、MCP 这些内部配置——那属于管理后台。
 */

import { Composer } from '../components/composer.js';
import { Icon } from '../components/icons.js';
import { EmptyState, SearchInput } from '../components/ui.js';
import { actions, state, toast } from '../store.js';
import { navigate } from '../router.js';

const GROUPS = [
  { key: 'official', label: '官方' },
  { key: 'workspace', label: '企业' },
  { key: 'mine', label: '我的' },
];

export const AgentsView = {
  name: 'AgentsView',
  components: { Composer, EmptyState, Icon, SearchInput },
  props: { embedded: Boolean },
  setup() {
    return { navigate, state, toast };
  },
  data() {
    return { query: '', activeGroup: 'official' };
  },
  computed: {
    agents() {
      const keyword = this.query.trim().toLowerCase();
      return state.agents
        .filter(item => !keyword
          || item.name.toLowerCase().includes(keyword)
          || (item.description || '').toLowerCase().includes(keyword))
        .filter(item => {
          if (this.activeGroup === 'official') return item.builtin;
          if (this.activeGroup === 'mine') return item.created_by === (state.user?.id || 'local-default');
          return !item.builtin && item.visibility !== 'private';
        });
    },
    groups() {
      return GROUPS;
    },
  },
  methods: {
    countFor(key) {
      return state.agents.filter(item => {
        if (key === 'official') return item.builtin;
        if (key === 'mine') return item.created_by === (state.user?.id || 'local-default');
        return !item.builtin && item.visibility !== 'private';
      }).length;
    },
    start(agent) {
      navigate('workbench', { agent: agent.id });
    },
    ask(agent, text) {
      navigate('workbench', { agent: agent.id, ask: text });
    },
  },
  template: `
    <div :class="embedded ? '' : 'view--page'">
      <div class="view__inner">
        <header class="page-head">
          <div class="grow">
            <h1 class="page-head__title">智能体</h1>
            <p class="page-head__desc">选择适合业务场景的智能体，在授权数据范围内开展查询与分析。</p>
          </div>
        </header>

        <div class="toolbar">
          <div class="segmented">
            <button v-for="entry in groups" :key="entry.key" :class="{ active: entry.key === activeGroup }"
                    @click="activeGroup = entry.key">{{ entry.label }} {{ countFor(entry.key) }}</button>
          </div>
          <span class="toolbar__spacer"></span>
          <SearchInput v-model="query" placeholder="搜索智能体" />
        </div>

        <EmptyState v-if="!agents.length" icon="robot" :title="query ? '没有匹配的智能体' : '该分组暂无智能体'"
                    :text="query ? '尝试其他关键词，或切换分组查看。' : '请联系管理员在「管理后台 → 智能体」创建并发布。'" />

        <div v-else class="grid grid--2">
          <article v-for="agent in agents" :key="agent.id" class="card card--interactive agent-card">
            <div class="agent-card__top">
              <span class="agent-card__mark"><Icon :name="agent.icon || 'robot'" :size="20" /></span>
              <div class="grow">
                <div class="row row--between">
                  <b class="agent-card__name">{{ agent.name }}</b>
                  <span v-if="agent.builtin" class="badge badge--brand">官方</span>
                </div>
                <p class="agent-card__desc">{{ agent.description || '暂无描述' }}</p>
              </div>
            </div>

            <div v-if="agent.tags && agent.tags.length" class="tag-row">
              <span v-for="tag in agent.tags" :key="tag" class="badge">{{ tag }}</span>
            </div>

            <p v-if="agent.welcome" class="small muted">{{ agent.welcome }}</p>

            <div v-if="agent.suggested_questions && agent.suggested_questions.length" class="inline-list">
              <button v-for="text in agent.suggested_questions.slice(0, 3)" :key="text" class="chip"
                      @click="ask(agent, text)">{{ text }}</button>
            </div>

            <div class="agent-card__foot">
              <span class="xs faint">{{ agent.suggested_questions ? agent.suggested_questions.length : 0 }} 个推荐问题</span>
              <button class="btn btn--primary btn--sm" @click="start(agent)">开始使用<Icon name="arrowRight" :size="14" /></button>
            </div>
          </article>
        </div>
      </div>
    </div>`,
};
