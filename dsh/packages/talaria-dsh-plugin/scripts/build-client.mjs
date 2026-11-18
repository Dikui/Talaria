import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { build } from 'vite'

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const tempDir = resolve(packageDir, '.client-build')

await build({
  configFile: false,
  root: packageDir,
  plugins: [react()],
  build: {
    outDir: tempDir,
    emptyOutDir: true,
    target: 'es2022',
    minify: false,
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
    cssCodeSplit: false,
    lib: {
      entry: resolve(packageDir, 'client/index.tsx'),
      formats: ['cjs'],
      fileName: () => 'client.cjs',
    },
    rollupOptions: {
      external: ['react', 'react-dom', 'react/jsx-runtime', 'react-dom/client'],
    },
  },
})

const code = readFileSync(resolve(tempDir, 'client.cjs'), 'utf8')
const wrapped = `window.__ModuleLoader__.load({id:\`@talaria/dsh-plugin\`,factory:require=>{var module={exports:{}};var exports=module.exports;var process={env:{NODE_ENV:"production"}};\n${code}\nreturn module.exports;}});\n`
mkdirSync(resolve(packageDir, 'client'), { recursive: true })
writeFileSync(resolve(packageDir, 'client/index.js'), wrapped)
rmSync(tempDir, { recursive: true, force: true })
console.log('client bundle:', resolve(packageDir, 'client/index.js'))
