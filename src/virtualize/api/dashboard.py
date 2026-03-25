"""Serves the built-in web dashboard as static HTML from the API server."""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Virtualize</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    body { font-family: 'Inter', sans-serif; }
    .gradient-bg { background: linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%); }
    .card-glow { box-shadow: 0 0 20px rgba(59, 130, 246, 0.1); }
    .card-glow:hover { box-shadow: 0 0 30px rgba(59, 130, 246, 0.2); }
    .status-pulse { animation: pulse 2s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    .fade-in { animation: fadeIn 0.3s ease-in; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #1e293b; }
    ::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
  </style>
</head>
<body class="gradient-bg text-gray-100 min-h-screen">
<div id="root"></div>
<script type="text/babel">
const { useState, useEffect, useCallback, useRef } = React;

const API = '/api/v1';

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || err.message || 'Request failed');
  }
  return res.json();
}

// ── Icons (SVG inlined for zero deps) ──

const Icons = {
  Server: () => (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 01-3-3m3 3a3 3 0 100 6h13.5a3 3 0 100-6m-16.5-3a3 3 0 013-3h13.5a3 3 0 013 3m-19.5 0a4.5 4.5 0 01.9-2.7L5.737 5.1a3.375 3.375 0 012.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 01.9 2.7m0 0a3 3 0 01-3 3m0 3h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008zm-3 6h.008v.008h-.008v-.008zm0-6h.008v.008h-.008v-.008z" />
    </svg>
  ),
  Play: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z" />
    </svg>
  ),
  Stop: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 7.5A2.25 2.25 0 017.5 5.25h9a2.25 2.25 0 012.25 2.25v9a2.25 2.25 0 01-2.25 2.25h-9a2.25 2.25 0 01-2.25-2.25v-9z" />
    </svg>
  ),
  Trash: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
    </svg>
  ),
  Terminal: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z" />
    </svg>
  ),
  Shield: () => (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
    </svg>
  ),
  Plus: () => (
    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
    </svg>
  ),
  Cpu: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 3v1.5M4.5 8.25H3m18 0h-1.5M4.5 12H3m18 0h-1.5M4.5 15.75H3m18 0h-1.5M8.25 19.5V21M12 3v1.5m0 15V21m3.75-18v1.5m0 15V21m-9-1.5h10.5a2.25 2.25 0 002.25-2.25V6.75a2.25 2.25 0 00-2.25-2.25H6.75A2.25 2.25 0 004.5 6.75v10.5a2.25 2.25 0 002.25 2.25z" />
    </svg>
  ),
  Refresh: () => (
    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
    </svg>
  ),
};

// ── Status Badge ──

function StatusBadge({ status }) {
  const colors = {
    running: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
    stopped: 'bg-red-500/20 text-red-400 border-red-500/30',
    creating: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
    error: 'bg-red-500/20 text-red-300 border-red-500/50',
    destroyed: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
    starting: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
    stopping: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
    paused: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  };
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors[status] || colors.stopped}`}>
      {status === 'running' && <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 status-pulse" />}
      {status}
    </span>
  );
}

// ── Create VM Modal ──

function CreateVMModal({ open, onClose, onCreated }) {
  const [form, setForm] = useState({ name: '', vcpus: 2, memory_mb: 2048, disk_size_gb: 20, os_type: 'linux', gpu: 'none', network: 'nat' });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const vm = await api('/vms', { method: 'POST', body: JSON.stringify(form) });
      onCreated(vm);
      onClose();
      setForm({ name: '', vcpus: 2, memory_mb: 2048, disk_size_gb: 20, os_type: 'linux', gpu: 'none', network: 'nat' });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm fade-in">
      <div className="bg-slate-800 border border-slate-700 rounded-2xl p-6 w-full max-w-lg shadow-2xl fade-in">
        <h2 className="text-xl font-semibold mb-4 flex items-center gap-2"><Icons.Plus /> Create Virtual Machine</h2>
        {error && <div className="bg-red-500/20 text-red-300 border border-red-500/30 rounded-lg p-3 mb-4 text-sm">{error}</div>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">Name</label>
            <input required value={form.name} onChange={e => setForm({...form, name: e.target.value})}
              className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none" placeholder="my-dev-vm" />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm text-gray-400 mb-1">vCPUs</label>
              <input type="number" min="1" max="64" value={form.vcpus} onChange={e => setForm({...form, vcpus: +e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none" />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Memory (MB)</label>
              <input type="number" min="256" step="256" value={form.memory_mb} onChange={e => setForm({...form, memory_mb: +e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none" />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Disk (GB)</label>
              <input type="number" min="1" max="2048" value={form.disk_size_gb} onChange={e => setForm({...form, disk_size_gb: +e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm text-gray-400 mb-1">OS</label>
              <select value={form.os_type} onChange={e => setForm({...form, os_type: e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none">
                <option value="linux">Linux</option>
                <option value="windows">Windows</option>
                <option value="macos">macOS</option>
                <option value="other">Other</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">GPU</label>
              <select value={form.gpu} onChange={e => setForm({...form, gpu: e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none">
                <option value="none">None</option>
                <option value="virtual">Virtual</option>
                <option value="passthrough">Passthrough</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Network</label>
              <select value={form.network} onChange={e => setForm({...form, network: e.target.value})}
                className="w-full bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm focus:border-blue-500 focus:outline-none">
                <option value="nat">NAT</option>
                <option value="bridge">Bridge</option>
                <option value="isolated">Isolated</option>
                <option value="host">Host</option>
              </select>
            </div>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-white transition">Cancel</button>
            <button type="submit" disabled={loading}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium transition disabled:opacity-50">
              {loading ? 'Creating...' : 'Create VM'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Exec Modal ──

function ExecModal({ vm, open, onClose }) {
  const [command, setCommand] = useState('');
  const [output, setOutput] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleExec = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const result = await api(`/vms/${vm.id}/exec`, { method: 'POST', body: JSON.stringify({ command }) });
      setOutput(result);
    } catch (err) {
      setOutput({ stderr: err.message, exit_code: -1, stdout: '' });
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm fade-in">
      <div className="bg-slate-800 border border-slate-700 rounded-2xl p-6 w-full max-w-2xl shadow-2xl fade-in">
        <h2 className="text-xl font-semibold mb-4 flex items-center gap-2"><Icons.Terminal /> Execute in {vm.name}</h2>
        <form onSubmit={handleExec} className="flex gap-2 mb-4">
          <input value={command} onChange={e => setCommand(e.target.value)} placeholder="uname -a"
            className="flex-1 bg-slate-900 border border-slate-600 rounded-lg px-3 py-2 text-sm font-mono focus:border-blue-500 focus:outline-none" />
          <button type="submit" disabled={loading}
            className="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 rounded-lg text-sm font-medium transition disabled:opacity-50">
            {loading ? 'Running...' : 'Run'}
          </button>
        </form>
        {output && (
          <div className="bg-slate-900 rounded-lg p-4 font-mono text-xs max-h-80 overflow-auto">
            {output.stdout && <pre className="text-green-300 whitespace-pre-wrap">{output.stdout}</pre>}
            {output.stderr && <pre className="text-red-300 whitespace-pre-wrap">{output.stderr}</pre>}
            <div className="text-gray-500 mt-2 border-t border-slate-700 pt-2">
              Exit: {output.exit_code} | Duration: {output.duration_seconds}s {output.timed_out && '| TIMED OUT'}
            </div>
          </div>
        )}
        <div className="flex justify-end mt-4">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-white transition">Close</button>
        </div>
      </div>
    </div>
  );
}

// ── VM Card ──

function VMCard({ vm, onRefresh }) {
  const [loading, setLoading] = useState('');
  const [execOpen, setExecOpen] = useState(false);

  const action = async (act) => {
    setLoading(act);
    try {
      if (act === 'start') await api(`/vms/${vm.id}/start`, { method: 'POST' });
      else if (act === 'stop') await api(`/vms/${vm.id}/stop`, { method: 'POST' });
      else if (act === 'destroy') {
        if (!confirm(`Destroy ${vm.name}? This cannot be undone.`)) { setLoading(''); return; }
        await api(`/vms/${vm.id}`, { method: 'DELETE' });
      }
      onRefresh();
    } catch (err) {
      alert(err.message);
    } finally {
      setLoading('');
    }
  };

  return (
    <>
      <div className="bg-slate-800/80 border border-slate-700/50 rounded-xl p-5 card-glow transition-all fade-in">
        <div className="flex items-start justify-between mb-3">
          <div>
            <h3 className="font-semibold text-base">{vm.name}</h3>
            <span className="text-xs text-gray-500 font-mono">{vm.id}</span>
          </div>
          <StatusBadge status={vm.status} />
        </div>
        <div className="grid grid-cols-3 gap-2 text-xs text-gray-400 mb-4">
          <div className="flex items-center gap-1"><Icons.Cpu /> {vm.vcpus} vCPU</div>
          <div>{vm.memory_mb}MB RAM</div>
          <div>{vm.disk_gb}GB Disk</div>
          <div>GPU: {vm.gpu}</div>
          <div>Net: {vm.network}</div>
          <div>{vm.ssh_port ? `SSH :${vm.ssh_port}` : ''}</div>
        </div>
        <div className="flex gap-2">
          {vm.status !== 'running' && (
            <button onClick={() => action('start')} disabled={!!loading}
              className="flex items-center gap-1 px-3 py-1.5 bg-emerald-600/20 text-emerald-400 border border-emerald-600/30 rounded-lg text-xs hover:bg-emerald-600/30 transition disabled:opacity-50">
              <Icons.Play /> {loading === 'start' ? '...' : 'Start'}
            </button>
          )}
          {vm.status === 'running' && (
            <>
              <button onClick={() => action('stop')} disabled={!!loading}
                className="flex items-center gap-1 px-3 py-1.5 bg-orange-600/20 text-orange-400 border border-orange-600/30 rounded-lg text-xs hover:bg-orange-600/30 transition disabled:opacity-50">
                <Icons.Stop /> {loading === 'stop' ? '...' : 'Stop'}
              </button>
              <button onClick={() => setExecOpen(true)}
                className="flex items-center gap-1 px-3 py-1.5 bg-blue-600/20 text-blue-400 border border-blue-600/30 rounded-lg text-xs hover:bg-blue-600/30 transition">
                <Icons.Terminal /> Exec
              </button>
            </>
          )}
          <button onClick={() => action('destroy')} disabled={!!loading}
            className="flex items-center gap-1 px-3 py-1.5 bg-red-600/20 text-red-400 border border-red-600/30 rounded-lg text-xs hover:bg-red-600/30 transition disabled:opacity-50 ml-auto">
            <Icons.Trash /> {loading === 'destroy' ? '...' : 'Destroy'}
          </button>
        </div>
      </div>
      <ExecModal vm={vm} open={execOpen} onClose={() => setExecOpen(false)} />
    </>
  );
}

// ── Compliance Panel ──

function CompliancePanel() {
  const [framework, setFramework] = useState('soc2');
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);

  const loadReport = async (fw) => {
    setLoading(true);
    try {
      const r = await api(`/compliance/report/${fw}`);
      setReport(r);
    } catch (err) {
      setReport(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadReport(framework); }, [framework]);

  return (
    <div className="fade-in">
      <div className="flex items-center gap-3 mb-6">
        <Icons.Shield />
        <h2 className="text-xl font-semibold">Compliance</h2>
        <div className="flex gap-1 ml-4">
          {['soc1', 'soc2', 'soc3', 'hipaa', 'iso27001'].map(fw => (
            <button key={fw} onClick={() => { setFramework(fw); }}
              className={`px-3 py-1 rounded-lg text-xs font-medium transition ${framework === fw ? 'bg-blue-600 text-white' : 'bg-slate-700 text-gray-400 hover:text-white'}`}>
              {fw.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {loading ? (
        <div className="text-gray-500 text-sm">Loading...</div>
      ) : report && (
        <div className="space-y-3">
          <div className="flex items-center gap-4 mb-4">
            <span className={`text-lg font-bold ${report.compliant ? 'text-emerald-400' : 'text-red-400'}`}>
              {report.compliant ? 'COMPLIANT' : 'NON-COMPLIANT'}
            </span>
            <span className="text-sm text-gray-400">
              {report.enabled_controls}/{report.total_controls} controls enabled
            </span>
          </div>
          {report.controls.map(ctrl => (
            <div key={ctrl.id} className="bg-slate-800/80 border border-slate-700/50 rounded-lg p-4">
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium">{ctrl.id} — {ctrl.title}</span>
                <span className={`text-xs px-2 py-0.5 rounded-full ${ctrl.enabled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'}`}>
                  {ctrl.enabled ? 'Enabled' : 'Disabled'}
                </span>
              </div>
              <p className="text-xs text-gray-400">{ctrl.technical_control}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── System Info ──

function SystemInfo() {
  const [info, setInfo] = useState(null);
  useEffect(() => { api('/system/info').then(setInfo).catch(() => {}); }, []);
  if (!info) return null;
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
      {[
        { label: 'Platform', value: `${info.platform} ${info.arch}` },
        { label: 'CPUs', value: info.cpu_count },
        { label: 'Memory', value: `${Math.round(info.memory_available_mb/1024)}/${Math.round(info.memory_total_mb/1024)} GB` },
        { label: 'QEMU', value: info.qemu_available ? 'Installed' : 'Not Found', color: info.qemu_available ? 'text-emerald-400' : 'text-red-400' },
      ].map(({ label, value, color }) => (
        <div key={label} className="bg-slate-800/60 border border-slate-700/40 rounded-lg p-3 text-center">
          <div className="text-xs text-gray-500 mb-1">{label}</div>
          <div className={`text-sm font-medium ${color || ''}`}>{value}</div>
        </div>
      ))}
    </div>
  );
}

// ── App ──

function App() {
  const [page, setPage] = useState('vms');
  const [vms, setVms] = useState([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const loadVMs = useCallback(async () => {
    try {
      const list = await api('/vms');
      setVms(list);
    } catch (err) { console.error(err); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { loadVMs(); const t = setInterval(loadVMs, 5000); return () => clearInterval(t); }, [loadVMs]);

  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="border-b border-slate-700/50 bg-slate-900/50 backdrop-blur-xl sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-gradient-to-br from-blue-500 to-purple-600 rounded-lg flex items-center justify-center">
              <Icons.Server />
            </div>
            <h1 className="text-xl font-bold tracking-tight">Virtualize</h1>
            <span className="text-xs text-gray-500 bg-slate-800 px-2 py-0.5 rounded-full">v0.1.0</span>
          </div>
          <nav className="flex gap-1">
            {[
              { id: 'vms', label: 'Machines' },
              { id: 'compliance', label: 'Compliance' },
            ].map(({ id, label }) => (
              <button key={id} onClick={() => setPage(id)}
                className={`px-4 py-2 rounded-lg text-sm transition ${page === id ? 'bg-slate-700 text-white' : 'text-gray-400 hover:text-white'}`}>
                {label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-7xl mx-auto px-6 py-8">
        <SystemInfo />

        {page === 'vms' && (
          <div className="fade-in">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-semibold flex items-center gap-2"><Icons.Server /> Virtual Machines</h2>
              <div className="flex gap-2">
                <button onClick={loadVMs} className="p-2 text-gray-400 hover:text-white transition rounded-lg hover:bg-slate-700">
                  <Icons.Refresh />
                </button>
                <button onClick={() => setCreateOpen(true)}
                  className="flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium transition">
                  <Icons.Plus /> New VM
                </button>
              </div>
            </div>

            {loading ? (
              <div className="text-center text-gray-500 py-20">Loading...</div>
            ) : vms.length === 0 ? (
              <div className="text-center py-20">
                <div className="text-gray-500 mb-4 text-6xl">&#x1F5A5;</div>
                <h3 className="text-lg font-medium text-gray-300 mb-2">No virtual machines</h3>
                <p className="text-gray-500 text-sm mb-4">Create your first VM to get started</p>
                <button onClick={() => setCreateOpen(true)}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg text-sm font-medium transition">
                  Create VM
                </button>
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {vms.map(vm => <VMCard key={vm.id} vm={vm} onRefresh={loadVMs} />)}
              </div>
            )}
            <CreateVMModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={loadVMs} />
          </div>
        )}

        {page === 'compliance' && <CompliancePanel />}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-800 mt-20 py-6 text-center text-xs text-gray-600">
        Virtualize v0.1.0 — Free, cross-platform VM orchestration for AI workflows
      </footer>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
</body>
</html>"""


def get_dashboard_html() -> str:
    return DASHBOARD_HTML
