import {useCallback, useEffect, useState} from 'react'
import {api} from '../api/client'
import type {ConfigSave, ConfigView} from '../api/types'
import ErrorToast from '../components/ErrorToast'

export default function SettingsPage() {
    const [loaded, setLoaded] = useState(false)

    const [lightBaseUrl, setLightBaseUrl] = useState('')
    const [lightModel, setLightModel] = useState('')
    const [lightApiKey, setLightApiKey] = useState('')
    const [powerBaseUrl, setPowerBaseUrl] = useState('')
    const [powerModel, setPowerModel] = useState('')
    const [powerApiKey, setPowerApiKey] = useState('')

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
    const [contextWindow, setContextWindow] = useState('')
    const [initialProjectRoot, setInitialProjectRoot] = useState('')
    const [channelInput, setChannelInput] = useState('')
    const [channelType, setChannelType] = useState('')
    const [hasChannel, setHasChannel] = useState(false)
    const [toast, setToast] = useState<{ message: string; variant: 'error' | 'success' } | null>(null)
    const [busy, setBusy] = useState(false)

    useEffect(() => {
        let alive = true
        api
            .getConfig()
            .then((cfg: ConfigView) => {
                if (!alive) return

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
                setChannelInput('')
                setToast({message: `已导入通道（${obj._type}）：URL / Key 已自动填充两组，请确认模型名后保存`, variant: 'success'})
            }
        } catch {

        }
    }, [])

    const handleSave = useCallback(async () => {
        if (!lightModel.trim() || !powerModel.trim()) {
            setToast({message: '请填写 light 与 power 两个模型', variant: 'error'})
            return
        }

        const ctx = Number(contextWindow)
        if (!Number.isInteger(ctx) || ctx < 1000) {
            setToast({message: '上下文窗口必须是 ≥1000 的整数', variant: 'error'})
            return
        }

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
                    {}
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
