// ═══════════════════════════════════════════════════════════════
// SettingsPage（SWP4-D / WP4-4 §3.10，改写 SWP4-A 占位）
// - 表单字段（与 WP3 GET /api/config 对齐）：light/power 两组——
//   Base URL、Model、API Key（密码框，留空保持原 Key）、三个价格
//   （未缓存输入/缓存输入/输出，不显示单位——设置口径与展示一致）
// - 2026-08-23：light/power 独立端点与 Key（旧共享 baseUrl/apiKey
//   字段不再显示，后端兼容回退 + data patch 自动迁移）
// - API Key：密码输入框；placeholder="留空则保持原 Key"；保存时为空串 →
//   后端保留旧值（payload 仍发送 apiKey:''）；Key 不回显（getConfig 仅返回
//   hasLightApiKey/hasPowerApiKey 布尔，禁止渲染进 DOM）
// - 「测试连接」→ api.testLlm() → 结果提示
// - 保存前校验（J10）：lightModel/powerModel 任一为空 → 阻止保存并提示
// - 「保存」→ api.saveConfig({...}) → 成功提示；projectRoot 变更 →
//   提示"需重启服务生效（运行 start.bat 前请确认）"
// ═══════════════════════════════════════════════════════════════

import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {ConfigSave, ConfigView} from '../api/types'
import ErrorToast from '../components/ErrorToast'

export default function SettingsPage() {
    const [loaded, setLoaded] = useState(false)
    // Light / Power 两组独立端点与 Key
    const [lightBaseUrl, setLightBaseUrl] = useState('')
    const [lightModel, setLightModel] = useState('')
    const [lightApiKey, setLightApiKey] = useState('') // 不回显：仅作输入（空串 = 保持旧 Key）
    const [powerBaseUrl, setPowerBaseUrl] = useState('')
    const [powerModel, setPowerModel] = useState('')
    const [powerApiKey, setPowerApiKey] = useState('') // 不回显：仅作输入（空串 = 保持旧 Key）
    // 六项价格（未缓存输入/缓存输入/输出 × light/power；不显示单位）
    const [lightInputPrice, setLightInputPrice] = useState('')
    const [lightCachedPrice, setLightCachedPrice] = useState('')
    const [lightOutputPrice, setLightOutputPrice] = useState('')
    const [powerInputPrice, setPowerInputPrice] = useState('')
    const [powerCachedPrice, setPowerCachedPrice] = useState('')
    const [powerOutputPrice, setPowerOutputPrice] = useState('')
    const [hasLightApiKey, setHasLightApiKey] = useState(false)
    const [hasPowerApiKey, setHasPowerApiKey] = useState(false)
    const [projectRoot, setProjectRoot] = useState('')
    const [port, setPort] = useState('')
    const [host, setHost] = useState('')
    const [contextWindow, setContextWindow] = useState('') // 上下文窗口总容量（Token 展示）
    const [initialProjectRoot, setInitialProjectRoot] = useState('') // 变更检测（重启提示）
    const [channelInput, setChannelInput] = useState('') // 2026-08-19：New API 通道 JSON 粘贴框（不回显）
    const [channelType, setChannelType] = useState('') // 当前生效的 llmChannel 通道类型
    const [hasChannel, setHasChannel] = useState(false) // llmChannel 已配置
    const [toast, setToast] = useState<{ message: string; variant: 'error' | 'success' } | null>(null)
    const [busy, setBusy] = useState(false)

    // 加载回填（getConfig；apiKey 不返回，仅 hasLightApiKey/hasPowerApiKey 指示）
    useEffect(() => {
        let alive = true
        api
            .getConfig()
            .then((cfg: ConfigView) => {
                if (!alive) return
                // 2026-08-23：新字段（后端已做旧共享字段回退填充/自动迁移）
                setLightBaseUrl(cfg.lightBaseUrl ?? cfg.baseUrl ?? '')
                setLightModel(cfg.lightModel)
                setPowerBaseUrl(cfg.powerBaseUrl ?? cfg.baseUrl ?? '')
                setPowerModel(cfg.powerModel)
                setHasLightApiKey(cfg.hasLightApiKey ?? cfg.hasApiKey)
                setHasPowerApiKey(cfg.hasPowerApiKey ?? cfg.hasApiKey)
                setLightInputPrice(String(cfg.lightInputPrice ?? 0))
                setLightCachedPrice(String(cfg.lightCachedPrice ?? 0))
                setLightOutputPrice(String(cfg.lightOutputPrice ?? 0))
                setPowerInputPrice(String(cfg.powerInputPrice ?? 0))
                setPowerCachedPrice(String(cfg.powerCachedPrice ?? 0))
                setPowerOutputPrice(String(cfg.powerOutputPrice ?? 0))
                setProjectRoot(cfg.projectRoot)
                setPort(String(cfg.port))
                setHost(cfg.host)
                setContextWindow(String(cfg.contextWindow ?? 400000))
                setChannelType(cfg.channelType ?? '')
                setHasChannel(cfg.hasChannel ?? false)
                setInitialProjectRoot(cfg.projectRoot)
                setLoaded(true)
            })
            .catch((err) => {
                if (!alive) return
                setToast({message: err instanceof Error ? err.message : '配置加载失败', variant: 'error'})
            })
        return () => {
            alive = false
        }
    }, [])

    /**
     * 2026-08-19：New API 通道 JSON 一键导入（newapi_channel_conn）。
     * 粘贴 {"_type":"newapi_channel_conn","key":"sk-...","url":"https://..."}
     * → 自动填充 Light/Power 两组的 Base URL / API Key（同一网关）。
     */
    const handleChannelInput = useCallback((raw: string) => {
        setChannelInput(raw)
        const trimmed = raw.trim()
        if (!trimmed) return
        try {
            const obj = JSON.parse(trimmed)
            if (obj && typeof obj === 'object' && typeof obj._type === 'string' && obj._type.toLowerCase().includes('conn')) {
                if (typeof obj.url === 'string' && obj.url) {
                    setLightBaseUrl(obj.url)
                    setPowerBaseUrl(obj.url)
                }
                if (typeof obj.key === 'string' && obj.key) {
                    setLightApiKey(obj.key)
                    setPowerApiKey(obj.key)
                }
                setChannelInput('') // 解析成功清空输入框（Key 不回显）
                setToast({message: `已导入通道（${obj._type}）：URL / Key 已自动填充两组，请确认模型名后保存`, variant: 'success'})
            }
        } catch {
            // 粘贴未完成/非法 JSON：保持现状
        }
    }, [])

    /** §3.10 J10：保存前校验——lightModel/powerModel 任一为空 → 阻止保存 */
    const handleSave = useCallback(async () => {
        if (!lightModel.trim() || !powerModel.trim()) {
            setToast({message: '请填写 light 与 power 两个模型', variant: 'error'})
            return
        }
        // Token 展示：contextWindow 必须是 ≥1000 的正整数
        const ctx = Number(contextWindow)
        if (!Number.isInteger(ctx) || ctx < 1000) {
            setToast({message: '上下文窗口必须是 ≥1000 的整数', variant: 'error'})
            return
        }
        // 价格必须是非负数字（空串按 0 处理）
        const prices: Record<string, string> = {
            lightInputPrice, lightCachedPrice, lightOutputPrice,
            powerInputPrice, powerCachedPrice, powerOutputPrice,
        }
        for (const [k, v] of Object.entries(prices)) {
            const n = Number(v === '' ? 0 : v)
            if (!Number.isFinite(n) || n < 0) {
                setToast({message: `价格必须是 ≥0 的数字（${k}）`, variant: 'error'})
                return
            }
        }
        setBusy(true)
        try {
            // apiKey 类空串也发送（后端保留旧值）
            const payload: ConfigSave = {
                lightModel, powerModel, projectRoot, apiKey: '',
                lightBaseUrl, lightApiKey, powerBaseUrl, powerApiKey,
                lightInputPrice: Number(lightInputPrice === '' ? 0 : lightInputPrice),
                lightCachedPrice: Number(lightCachedPrice === '' ? 0 : lightCachedPrice),
                lightOutputPrice: Number(lightOutputPrice === '' ? 0 : lightOutputPrice),
                powerInputPrice: Number(powerInputPrice === '' ? 0 : powerInputPrice),
                powerCachedPrice: Number(powerCachedPrice === '' ? 0 : powerCachedPrice),
                powerOutputPrice: Number(powerOutputPrice === '' ? 0 : powerOutputPrice),
                contextWindow: ctx,
            }
            await api.saveConfig(payload)
            // projectRoot 变更 → 重启提示
            if (projectRoot !== initialProjectRoot) {
                setToast({message: '需重启服务生效（运行 start.bat 前请确认）', variant: 'success'})
            } else {
                setToast({message: '已保存', variant: 'success'})
            }
        } catch (err) {
            setToast({message: err instanceof Error ? err.message : '保存失败', variant: 'error'})
        } finally {
            setBusy(false)
        }
    }, [lightModel, powerModel, projectRoot, contextWindow, initialProjectRoot,
        lightBaseUrl, lightApiKey, powerBaseUrl, powerApiKey,
        lightInputPrice, lightCachedPrice, lightOutputPrice,
        powerInputPrice, powerCachedPrice, powerOutputPrice])

    /** §3.10「测试连接」→ api.testLlm() → 结果提示 */
    const handleTest = useCallback(async () => {
        setBusy(true)
        try {
            const r = await api.testLlm()
            if (r.ok) {
                setToast({message: `连接成功（${r.model ?? '模型'}）`, variant: 'success'})
            } else {
                setToast({message: `连接失败：${r.error ?? '未知错误'}`, variant: 'error'})
            }
        } catch (err) {
            setToast({message: err instanceof Error ? err.message : '测试连接失败', variant: 'error'})
        } finally {
            setBusy(false)
        }
    }, [])

    /** 单个模型组表单（Base URL / Model / API Key / 三个价格） */
    const renderGroup = (group: 'light' | 'power') => {
        const isLight = group === 'light'
        const title = isLight ? 'Light（轻量模型）' : 'Power（强力模型）'
        const baseUrl = isLight ? lightBaseUrl : powerBaseUrl
        const model = isLight ? lightModel : powerModel
        const hasKey = isLight ? hasLightApiKey : hasPowerApiKey
        const setUrl = isLight ? setLightBaseUrl : setPowerBaseUrl
        const setModel = isLight ? setLightModel : setPowerModel
        const setKey = isLight ? setLightApiKey : setPowerApiKey
        const inP = isLight ? lightInputPrice : powerInputPrice
        const cachedP = isLight ? lightCachedPrice : powerCachedPrice
        const outP = isLight ? lightOutputPrice : powerOutputPrice
        const setIn = isLight ? setLightInputPrice : setPowerInputPrice
        const setCached = isLight ? setLightCachedPrice : setPowerCachedPrice
        const setOut = isLight ? setLightOutputPrice : setPowerOutputPrice
        return (
            <>
                <div className="settings-group-title">{title}</div>
                <div className="field-row">
                    <div className="field-label">Base URL</div>
                    <input className="text-input" value={baseUrl} onChange={(e) => setUrl(e.target.value)}
                           placeholder="https://api.openai.com/v1"/>
                </div>
                <div className="field-row">
                    <div className="field-label">{isLight ? 'Light Model' : 'Power Model'}</div>
                    <input className="text-input" value={model} onChange={(e) => setModel(e.target.value)}
                           placeholder={isLight ? 'gpt-4o-mini' : 'gpt-4o'}/>
                </div>
                <div className="field-row">
                    <div className="field-label">API Key {hasKey ? '（已配置）' : '（未配置）'}</div>
                    {/* Key 不回显：密码框 + 留空保持旧 Key */}
                    <input className="text-input" type="password" value={isLight ? lightApiKey : powerApiKey}
                           onChange={(e) => setKey(e.target.value)}
                           placeholder="留空则保持原 Key" autoComplete="off"/>
                </div>
                <div className="field-row price-row">
                    <div className="field-label">未缓存输入价格</div>
                    <input className="text-input price-input" type="number" min={0} step={0.1} value={inP}
                           onChange={(e) => setIn(e.target.value)} placeholder="0.5"/>
                </div>
                <div className="field-row price-row">
                    <div className="field-label">缓存输入价格</div>
                    <input className="text-input price-input" type="number" min={0} step={0.1} value={cachedP}
                           onChange={(e) => setCached(e.target.value)} placeholder="0.1"/>
                </div>
                <div className="field-row price-row">
                    <div className="field-label">输出价格</div>
                    <input className="text-input price-input" type="number" min={0} step={0.1} value={outP}
                           onChange={(e) => setOut(e.target.value)} placeholder="1.5"/>
                </div>
            </>
        )
    }

    return (
        <div className="content-area">
            <div className="step-header">设置</div>
            {!loaded && <div className="empty-state">加载中...</div>}
            {loaded && (
                <div className="settings-form">
                    {hasChannel && (
                        <div className="settings-hint">当前使用通道：{channelType}（来自 config.json llmChannel，未配新字段时回退）</div>
                    )}
                    <div className="field-row">
                        <div className="field-label">New API 通道（可选）</div>
                        <textarea
                            className="text-input"
                            rows={2}
                            value={channelInput}
                            onChange={(e) => handleChannelInput(e.target.value)}
                            placeholder={'粘贴通道 JSON 自动填充两组，如 {"_type":"newapi_channel_conn","key":"sk-...","url":"https://..."}'}
                        />
                    </div>
                    {renderGroup('light')}
                    {renderGroup('power')}
                    <div className="field-row">
                        <div className="field-label">项目目录（Project Root）</div>
                        <input className="text-input" value={projectRoot} onChange={(e) => setProjectRoot(e.target.value)} placeholder="workspace"/>
                    </div>
                    <div className="field-row">
                        <div className="field-label">服务端口（只读）</div>
                        <input className="text-input" value={port} readOnly/>
                    </div>
                    <div className="field-row">
                        <div className="field-label">上下文窗口大小（Context Window）</div>
                        <input
                            className="text-input"
                            type="number"
                            min={1000}
                            step={1000}
                            value={contextWindow}
                            onChange={(e) => setContextWindow(e.target.value)}
                            placeholder="400000"
                            title="模型上下文总容量（token），用于输入框上方的占用百分比计算"
                        />
                    </div>
                    <div className="field-row">
                        <div className="field-label">监听地址（只读）</div>
                        <input className="text-input" value={host} readOnly/>
                    </div>
                    <div className="settings-actions">
                        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
                            保存
                        </button>
                        <button className="btn" onClick={handleTest} disabled={busy}>
                            测试连接
                        </button>
                    </div>
                </div>
            )}
            {toast && <ErrorToast message={toast.message} variant={toast.variant} onClose={() => setToast(null)}/>}
        </div>
    )
}
