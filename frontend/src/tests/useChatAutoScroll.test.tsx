import {act, fireEvent, renderHook} from '@testing-library/react'
import {afterEach, beforeEach, describe, expect, it} from 'vitest'
import {useChatAutoScroll} from '../hooks/useChatAutoScroll'

function mockDims(el: HTMLElement, scrollHeight: number, clientHeight: number): void {
  Object.defineProperty(el, 'scrollHeight', {value: scrollHeight, configurable: true})
  Object.defineProperty(el, 'clientHeight', {value: clientHeight, configurable: true})
}

describe('useChatAutoScroll（显式开关版）', () => {
  let container: HTMLDivElement
  let deps: unknown[]

  beforeEach(() => {
    container = document.createElement('div')
    mockDims(container, 800, 400)
    deps = ['init']
  })

  afterEach(() => {

    Object.defineProperty(container, 'scrollHeight', {value: 0, configurable: true})
    Object.defineProperty(container, 'clientHeight', {value: 0, configurable: true})
  })

  it('开关开（默认）→ 挂载及内容更新均强制滚到底部（锁定跟随）', () => {
    const {rerender} = renderHook(() => useChatAutoScroll({current: container}, deps, true))

    expect(container.scrollTop).toBe(800)

    container.scrollTop = 0
    deps = ['chunk-1']
    rerender()
    expect(container.scrollTop).toBe(800)
  })

  it('开关关 → 内容更新完全不干预（用户自由滚动位置保持不变）', () => {
    const {rerender} = renderHook(() => useChatAutoScroll({current: container}, deps, false))

    expect(container.scrollTop).toBe(0)

    container.scrollTop = 250
    deps = ['chunk-2']
    rerender()
    expect(container.scrollTop).toBe(250)

    container.scrollTop = 0
    deps = ['chunk-3']
    rerender()
    expect(container.scrollTop).toBe(0)
  })

  it('开→关切换：切换后内容更新不再干预（自由滚动）', () => {
    const {rerender} = renderHook(({on}) => useChatAutoScroll({current: container}, deps, on), {
      initialProps: {on: true},
    })
    expect(container.scrollTop).toBe(800)

    container.scrollTop = 120
    rerender({on: false})
    expect(container.scrollTop).toBe(120)
    deps = ['chunk-4']
    rerender({on: false})
    expect(container.scrollTop).toBe(120)
  })

  it('关→开切换：恢复锁定跟随，后续内容更新强制滚底', () => {
    const {rerender} = renderHook(({on}) => useChatAutoScroll({current: container}, deps, on), {
      initialProps: {on: false},
    })
    container.scrollTop = 200
    rerender({on: true})
    expect(container.scrollTop).toBe(800)
    deps = ['chunk-5']
    rerender({on: true})
    expect(container.scrollTop).toBe(800)
  })

  it('容器缺失 → 安全跳过不抛错', () => {
    expect(() =>
        renderHook(() => useChatAutoScroll({current: null}, deps, true)),
    ).not.toThrow()
  })

  it('jumpToBottom：无论开关状态，显式点击立即滚到底部', () => {

    const {result} = renderHook(() => useChatAutoScroll({current: container}, deps, false))
    container.scrollTop = 200
    act(() => result.current.jumpToBottom())
    expect(container.scrollTop).toBe(800)

    const {result: resultOn} = renderHook(() => useChatAutoScroll({current: container}, deps, true))
    container.scrollTop = 0
    act(() => resultOn.current.jumpToBottom())
    expect(container.scrollTop).toBe(800)
  })

  it('showJump：滚离底部超阈值（100px）显示，接近底部隐藏，jumpToBottom 后隐藏', () => {
    const {result} = renderHook(() => useChatAutoScroll({current: container}, deps, false))

    expect(result.current.showJump).toBe(true)

    container.scrollTop = 750
    act(() => fireEvent.scroll(container))
    expect(result.current.showJump).toBe(false)

    container.scrollTop = 0
    act(() => fireEvent.scroll(container))
    expect(result.current.showJump).toBe(true)

    act(() => result.current.jumpToBottom())
    expect(container.scrollTop).toBe(800)
    expect(result.current.showJump).toBe(false)
  })
})
