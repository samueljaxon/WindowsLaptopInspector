import { spawn } from 'node:child_process'
import { realpathSync } from 'node:fs'
import { writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

export const inject = ['tools']

const INSPECTOR_PY = realpathSync(fileURLToPath(new URL('./inspector.py', import.meta.url)))

function runInspector(extraArgs, signal) {
  return new Promise((resolve, reject) => {
    const python = process.env.WINDOWS_LAPTOP_INSPECTOR_PYTHON || 'python'
    const child = spawn(python, [INSPECTOR_PY, ...extraArgs], {
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      signal,
    })
    let stdout = ''
    let stderr = ''
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (d) => { stdout += d })
    child.stderr.on('data', (d) => { stderr += d })
    child.on('error', (err) => reject(err))
    child.on('close', (code) => {
      if (code === 0) {
        try {
          resolve(JSON.parse(stdout))
        } catch (err) {
          reject(new Error(`Inspector returned invalid JSON: ${err.message}`))
        }
      } else {
        reject(new Error(`Inspector exited with code ${code}: ${stderr.trim() || 'no output'}`))
      }
    })
  })
}

function fmt(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : String(value)
}

function summarize(report) {
  const lines = []
  const machine = report.machine || {}
  const os = report.os || {}
  const cpu = report.cpu || {}
  const mem = report.memory || {}
  const battery = report.battery || {}
  const health = report.health || {}
  const perf = report.performance || {}
  const title = `${machine.manufacturer || ''} ${machine.model || machine.hostname || 'Windows laptop'}`.trim()

  lines.push(`**${title}** — ${os.product_name || 'Windows'} (build ${os.build || '?'})`)
  if (health.score !== null && health.score !== undefined) {
    lines.push(`Health score: **${health.score}/100** (grade ${health.grade || '?'})`)
  }
  lines.push(`- CPU: ${fmt(cpu.name)} (${fmt(cpu.physical_cores)}C/${fmt(cpu.logical_processors)}T)`)
  lines.push(`- RAM: ${fmt(mem.total_gb)} GB (${fmt(mem.used_percent)}% in use)`)
  for (const g of report.gpu || []) lines.push(`- GPU: ${fmt(g.name)}`)
  for (const d of report.storage || []) {
    lines.push(`- Storage: ${fmt(d.model)} · ${fmt(d.media_type)} · ${fmt(d.size_gb)} GB · ${fmt(d.health_status)}`)
  }
  if (battery.present) {
    lines.push(`- Battery: ${fmt(battery.health_percent)}% health (${fmt(battery.health_category)}) · ${fmt(battery.cycle_count)} cycles · ${fmt(battery.charge_percent)}% charged`)
    if (battery.discharge_rate_w) lines.push(`- Discharge rate: ${fmt(battery.discharge_rate_w)} W · estimated runtime ~${fmt(battery.estimated_runtime_minutes)} min`)
  } else {
    lines.push('- Battery: not detected')
  }
  if (perf.overall !== null && perf.overall !== undefined) {
    lines.push(`- Performance: ${fmt(perf.overall)}/100 — office ${fmt(perf.office)}, gaming ${fmt(perf.gaming)}, video editing ${fmt(perf.video_editing)}`)
  }
  const recs = report.recommendations || []
  if (recs.length) {
    lines.push('')
    lines.push('**Recommendations**')
    for (const r of recs) lines.push(`- [${r.level}] ${r.message}`)
  }
  const exported = (report.export || {})
  const paths = [exported.html, exported.pdf, exported.json].filter(Boolean)
  if (paths.length) lines.push(`\nExported: ${paths.join(', ')}`)
  return lines.join('\n')
}

// Raw tool definition: registered without the dsh-tools helper so the bundle
// has no dependency on shipped packages (a local bundle cannot import them).
const definition = {
  name: 'inspect_windows_laptop',
  description:
    'Run a full hardware and software inspection of this Windows laptop using native tools ' +
    '(PowerShell, WMI/CIM, powercfg) plus psutil, and return a structured report with battery ' +
    'health, a 0-100 overall health score, a performance estimate, and plain-language ' +
    'recommendations. Use it when the user wants to check a machine they are about to buy or ' +
    'diagnose one they own. Optionally export the report to a file as HTML (interactive dashboard ' +
    'with gauges) or PDF.',
  parameters: {
    type: 'object',
    properties: {
      format: {
        type: 'string',
        enum: ['json', 'html', 'pdf'],
        description: 'Export format to also write to disk. Defaults to json (return the report only).',
      },
      output_path: {
        type: 'string',
        description: 'File path to write the export to. Defaults to a timestamped file in the working directory.',
      },
      lang: {
        type: 'string',
        enum: ['en', 'my'],
        description: 'Language for the plain-language summary and recommendations. Defaults to en (English); my is Burmese.',
      },
    },
  },
  output: {
    schema: { type: 'object', additionalProperties: true },
    render: (_args, report) => [{ type: 'text', text: summarize(report) }],
    presentationMeta: (_args, report) => ({
      device: (report.machine?.model || report.machine?.hostname || 'Windows laptop'),
      healthScore: report.health?.score ?? null,
      healthGrade: report.health?.grade ?? null,
      batteryHealth: report.battery?.health_percent ?? null,
      batteryCategory: report.battery?.health_category ?? null,
      cycleCount: report.battery?.cycle_count ?? null,
      cpu: report.cpu?.name ?? null,
      ramGb: report.memory?.total_gb ?? null,
    }),
  },
  timeoutMs: 180000,
  async execute(args, exec) {
    const format = ['html', 'pdf'].includes(args?.format) ? args.format : null
    const lang = ['en', 'my'].includes(args?.lang) ? args.lang : null
    const outputPath = typeof args?.output_path === 'string' ? args.output_path : null

    const argv = ['--format', 'json']
    if (lang) argv.push('--lang', lang)
    if (format) {
      argv[1] = format // replace 'json' with the requested export format
      if (outputPath) argv.push('--output', outputPath)
    }
    const report = await runInspector(argv, exec.signal)
    if (!format && outputPath) {
      await writeFile(outputPath, JSON.stringify(report, null, 2), {
        encoding: 'utf8',
        signal: exec.signal,
      })
      report.export = { ...(report.export || {}), json: outputPath }
    }
    return report
  },
}

export function apply(ctx) {
  ctx.tools.register(definition)
}
