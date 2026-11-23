window.__ModuleLoader__.load({
  id: '@local/windows-laptop-inspector',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState } = React

    function InspectorDock() {
      const [open, setOpen] = useState(false)
      const chip = h('button', {
        type: 'button',
        onClick: () => setOpen((v) => !v),
        'aria-expanded': open,
        title: 'Windows Laptop Inspector',
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '4px 12px',
          borderRadius: 999,
          fontSize: 12,
          lineHeight: 1.4,
          cursor: 'pointer',
          background: 'transparent',
          color: 'var(--dsw-alias-text-secondary, currentColor)',
          border: '1px solid var(--dsw-alias-border-secondary, rgba(127,140,168,.35))',
        },
      },
        h('span', { 'aria-hidden': true }, '\u{1F50B}'),
        h('span', null, 'Laptop inspector'),
      )
      const hint = open ? h('div', {
        style: {
          fontSize: 12,
          lineHeight: 1.5,
          color: 'var(--dsw-alias-text-tertiary, rgba(127,140,168,.95))',
          maxWidth: 320,
        },
      }, 'Ask the agent to \u201Cinspect this laptop\u201D to run a full hardware, battery-health, and condition report.') : null
      return h('div', { style: { display: 'flex', alignItems: 'center', gap: 8 } }, chip, hint)
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
          name: 'conversation.composer.dock',
          id: 'windows-laptop-inspector',
          order: 5,
        }, InspectorDock))
      },
    }
  },
})
