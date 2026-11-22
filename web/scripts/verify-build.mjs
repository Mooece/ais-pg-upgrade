import assert from 'node:assert/strict'
import { readFileSync, realpathSync } from 'node:fs'
import { dirname, isAbsolute, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

// Check the deployable build, not just Vue source or the archive checksum.
const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = realpathSync(resolve(process.argv[2] || resolve(webRoot, '../src/aistudio_api/static')))
function readAsset(name) {
  assert(!isAbsolute(name), `Absolute asset path: ${name}`)
  const path = realpathSync(resolve(output, name))
  assert(path.startsWith(output + sep), `Asset outside build: ${name}`)
  return readFileSync(path, 'utf8')
}

const manifest = JSON.parse(readAsset('.vite/manifest.json'))
const entry = manifest['index.html']
assert(entry?.isEntry, 'Missing Vite HTML entry')
assert(readAsset('index.html').includes(`/${entry.file}`), 'HTML does not load the built entry')
const visited = new Set()
function visit(key) {
  if (visited.has(key)) return
  visited.add(key)
  const chunk = manifest[key]
  assert(chunk, `Missing manifest chunk: ${key}`)
  readAsset(chunk.file)
  for (const css of chunk.css || []) readAsset(css)
  for (const asset of chunk.assets || []) readAsset(asset)
  for (const dependency of [...(chunk.imports || []), ...(chunk.dynamicImports || [])]) visit(dependency)
}
visit('index.html')
const dashboardKey = 'src/views/DashboardView.vue'
assert(visited.has(dashboardKey), 'Dashboard is unreachable from the HTML entry')
const dashboard = readAsset(manifest[dashboardKey].file)
for (const marker of ['reply-cache-title', '/api/reply-cache', '回复缓存', '保留轮数']) {
  assert(dashboard.includes(marker), `Dashboard build is missing ${marker}; rebuild before publishing`)
}
console.log(`Build verified: ${manifest[dashboardKey].file} includes the reply cache UI and API; ${visited.size} chunks resolve.`)
