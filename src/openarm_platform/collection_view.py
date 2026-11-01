"""Same-origin collection monitoring and archive playback, without device owners."""
import asyncio
import time

from openarm_runtime import __version__
from .collection import CollectionBrowser, STATE_LABELS, archive_state


class CollectionView:
    def __init__(self, root):
        from nicegui import ui
        self.ui, self.browser = ui, CollectionBrowser(root)
        self.timeline = None
        self.render_key = None
        self.cursor = 0.
        self.last_tick = time.monotonic()
        self.playing = self.updating = self.rendering = self.polling = self.loading = False
        self.images, self.tactiles, self.sequences = {}, {}, {}
        ui.colors(primary='#36735a')
        ui.add_css('''
            body {background:#f6f8f5;color:#263d32}
            .collection-workspace {max-width:1300px;width:100%;margin:auto;padding:24px;gap:18px}
            .collection-card {width:100%;border:1px solid #dfe7df;box-shadow:none!important;border-radius:12px}
            .collection-toolbar {width:100%;align-items:center;gap:14px;flex-wrap:wrap}
            .collection-metrics {width:100%;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
            .collection-metric {background:white;border:1px solid #dfe7df;border-radius:12px;padding:14px}
            .collection-media {display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));width:100%;gap:12px}
            @media(max-width:650px) {.collection-workspace{padding:14px}.collection-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
        ''')
        with ui.column().classes('collection-workspace'):
            with ui.row().classes('collection-toolbar'):
                with ui.column().classes('grow gap-1'):
                    ui.label('数采可视化').classes('text-2xl font-semibold')
                    ui.label(f'OpenArm {__version__} · 录制状态与多模态回放').classes('text-sm text-grey-7')
                ui.button('进入遥操录制', icon='precision_manufacturing', on_click=self.open_capture).props('outline data-testid=collection-capture')
            ui.label(str(self.browser.root)).classes('text-xs text-grey-7 break-all')
            with ui.row().classes('collection-metrics'):
                self.metrics = {}
                for key,label in [('state','录制状态'),('duration','本段时长'),('queue','待写队列'),('episodes','已有会话')]:
                    with ui.column().classes('collection-metric gap-1'):
                        ui.label(label).classes('text-xs text-grey-7')
                        self.metrics[key] = ui.label('—').classes('text-xl font-semibold').props(f'data-testid=collection-{key}')
            with ui.card().classes('collection-card'):
                self.publisher = ui.select({}, label='采集实例', on_change=self.refresh_live).classes('w-full')
                self.live_message = ui.label('正在读取录制状态…').classes('text-sm').props('data-testid=collection-live-message')
                columns=[dict(name=k,label=l,field=k,align='left') for k,l in
                         [('stream','数据流'),('state','更新状态'),('age','采样距现在'),('sequence','源序号')]]
                self.live_table = ui.table(columns=columns,rows=[],row_key='stream').classes('w-full').props('flat dense')
                ui.label('状态来自采集进程，每 0.5 秒更新；开始、结束和验收录制沿用遥操中的控制。').classes('text-xs text-grey-7')
            with ui.card().classes('collection-card'):
                ui.label('已有记录').classes('text-lg font-medium')
                with ui.row().classes('collection-toolbar'):
                    self.episode = ui.select({}, label='采集会话').classes('grow').props('data-testid=collection-episode')
                    ui.button('刷新列表',icon='refresh',on_click=self.refresh_episodes).props('flat data-testid=collection-refresh')
                    self.load_button = ui.button('加载回放',on_click=self.load).props('data-testid=collection-load')
                self.summary = ui.label('选择会话查看相机、触觉和关节记录。').classes('text-sm').props('data-testid=collection-summary')
                self.problems = ui.label('').classes('text-orange-9 whitespace-pre-wrap').props('data-testid=collection-quality')
                with ui.row().classes('collection-toolbar'):
                    self.play_button = ui.button('播放回放',icon='play_arrow',on_click=self.toggle_play).props('flat data-testid=collection-play')
                    self.slider = ui.slider(min=0,max=1,step=.01,value=0,on_change=self.seek).classes('grow').props('data-testid=collection-time')
                    self.time_label = ui.label('0.00 s')
                ui.label('使用光标之前最近的采样。回放窗口保留每路最新 2,000 条记录；图像为历史预览。').classes('text-xs text-grey-7')
            self.error = ui.label('').classes('text-red-8 whitespace-pre-wrap').props('role=alert data-testid=collection-error')
            self.media = ui.row().classes('collection-media').props('data-testid=collection-media')
            self.touch = ui.row().classes('collection-media')
            with ui.card().classes('collection-card'):
                with ui.row().classes('collection-toolbar'):
                    ui.label('关节目标与反馈').classes('text-lg font-medium grow')
                    self.joint = ui.select({},label='关节',on_change=self.render).classes('w-44')
                self.chart = ui.echart({'animation':False,'tooltip':{'trigger':'axis'},
                    'legend':{'data':['提交目标','反馈采样']},
                    'xAxis':{'type':'value','name':'秒'},'yAxis':{'type':'value','name':'角度（°）','scale':True},
                    'series':[{'name':'提交目标','type':'line','step':'end','showSymbol':False,'data':[],'color':'#b58b48'},
                              {'name':'反馈采样','type':'line','showSymbol':False,'data':[],'color':'#36735a'}]
                }).classes('w-full h-72').props('data-testid=collection-chart')
                columns=[dict(name=k,label=l,field=k,align='left') for k,l in
                         [('joint','关节'),('target','提交目标（°）'),('feedback','反馈采样（°）')]]
                self.joint_table = ui.table(columns=columns,rows=[],row_key='joint').classes('w-full').props('flat dense data-testid=collection-joints')
        self.play_button.disable()
        self.slider.disable()
        ui.timer(.1,self.refresh_episodes,once=True)
        ui.timer(.5,self.refresh_live)
        ui.timer(.25,self.render)

    async def open_capture(self):
        await self.ui.run_javascript('''(() => {
            const button = window.parent.document.getElementById('nav-teleop');
            if (button) button.click(); else window.location.assign('/#teleop');
        })()''')

    async def refresh_live(self, _=None):
        if self.polling:
            return
        self.polling = True
        try:
            values = await asyncio.to_thread(self.browser.live)
            options = {v['publisher']:f"{v.get('task') or '采集实例'} · {v['publisher'][:8]}" for v in values}
            selected = self.publisher.value if self.publisher.value in options else next(iter(options),None)
            if options != self.publisher.options:
                self.publisher.set_options(options,value=selected)
            value = next((v for v in values if v['publisher']==selected),None)
            if value is None:
                self.metrics['state'].set_text('未连接')
                self.live_message.set_text('未发现采集进程状态。采集目录需要与遥操的 collection profile 中 root 一致。')
                self.live_table.rows=[]
                self.live_table.update()
                return
            fresh = value['fresh']
            state = STATE_LABELS.get(value['state'],value['state']) if fresh or value['state']=='closed' else '状态过期'
            self.metrics['state'].set_text(state)
            self.metrics['duration'].set_text(f"{value.get('duration_s',0):.1f} s")
            self.metrics['queue'].set_text(f"{value.get('queue_items',0)} 条")
            self.live_message.set_text(f"{value.get('task','')} · {value.get('operator','')} · {value.get('message','')}" +
                                      ('' if fresh else ' · 上次发布的状态，未确认当前是否仍在录制'))
            rows=[]
            for name,spec in value.get('streams',{}).items():
                age=spec.get('age_s')
                age=None if age is None else age+value['status_age_s']
                rows.append(dict(stream=name,state='更新中' if fresh and age is not None and 0<=age<=spec.get('max_age_s',.5) else '未更新',
                                 age='—' if age is None else f'{age*1000:.0f} ms',sequence=spec.get('sequence',0)))
            self.live_table.rows=rows
            self.live_table.update()
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self.live_message.set_text(f'状态读取失败：{exc}')
        finally:
            self.polling=False

    async def refresh_episodes(self):
        try:
            options=await asyncio.to_thread(self.browser.episodes)
            self.episode.set_options(options,value=self.episode.value if self.episode.value in options else next(iter(options),None))
            self.metrics['episodes'].set_text(str(len(options)))
        except (OSError,ValueError) as exc:
            self.error.set_text(str(exc))

    async def load(self):
        if self.loading:
            return
        self.loading=True
        self.load_button.disable()
        try:
            timeline=await asyncio.to_thread(self.browser.load,self.episode.value)
            self.timeline=timeline
            self.cursor=timeline.start
            self.playing=False
            self.sequences={}
            self.error.set_text('')
            manifest=timeline.manifest
            metadata=manifest.get('metadata',{})
            state=archive_state(manifest,timeline.review)
            self.summary.set_text(f"{metadata.get('task','未命名任务')} · {metadata.get('operator','')} · {state} · {sum(timeline.counts.values())} 条采样")
            self.problems.set_text('\n'.join(timeline.problems))
            self.images,self.tactiles={},{}
            self.media.clear()
            self.touch.clear()
            with self.media:
                for stream in timeline.streams:
                    if stream.startswith('camera/'):
                        with self.ui.card().classes('collection-card'):
                            self.ui.label(stream).classes('font-medium')
                            image=self.ui.image().classes('w-full h-48').props('fit=contain')
                            status=self.ui.label('该时间无采样').classes('text-xs text-grey-7')
                            self.images[stream]=(image,status)
            with self.touch:
                for stream in timeline.streams:
                    if stream.startswith('tactile/'):
                        with self.ui.card().classes('collection-card'):
                            self.ui.label(stream).classes('font-medium')
                            label=self.ui.label('该时间无采样').classes('text-sm')
                            chart=self.ui.echart({'animation':False,'tooltip':{'trigger':'axis'},
                                'legend':{'data':['X','Y','Z']},'xAxis':{'type':'category','name':'触点','data':list(range(68))},
                                'yAxis':{'type':'value','name':'原始计数'},
                                'series':[{'name':axis,'type':'line','showSymbol':False,'data':[]} for axis in ('X','Y','Z')]
                            }).classes('w-full h-52').props('data-testid=collection-tactile')
                            self.tactiles[stream]=(label,chart)
            self.joint.set_options({key:key.replace('robot/','').replace('/',' · J') for key in timeline.joints},value=next(iter(timeline.joints),None))
            self.slider._props.update(min=timeline.start,max=max(timeline.start+.01,timeline.end))
            self.play_button.enable()
            self.slider.enable()
            await self.render()
        except (OSError,ValueError,KeyError,TypeError,ImportError) as exc:
            self.error.set_text(f'记录加载失败：{exc}')
        finally:
            self.loading=False
            self.load_button.enable()

    def seek(self,event):
        if not self.updating:
            self.cursor=float(event.value)
            self.playing=False

    def toggle_play(self):
        if self.timeline is not None:
            self.playing=not self.playing
            if self.playing and self.cursor>=self.timeline.end:
                self.cursor=self.timeline.start
            self.last_tick=time.monotonic()

    async def render(self,_=None):
        if self.rendering or self.timeline is None:
            return
        self.rendering=True
        timeline=self.timeline
        try:
            now=time.monotonic()
            if self.playing:
                self.cursor=min(timeline.end,self.cursor+max(0,now-self.last_tick))
                if self.cursor>=timeline.end:
                    self.playing=False
            self.last_tick=now
            key=(id(timeline),self.cursor,self.joint.value,self.playing)
            if key==self.render_key:
                return
            self.render_key=key
            self.updating=True
            self.slider.value=self.cursor
            self.updating=False
            self.time_label.set_text(f'{self.cursor:.2f} s')
            self.play_button.set_text('暂停回放' if self.playing else '播放回放')
            for stream,(image,label) in self.images.items():
                sample=timeline.at(stream,self.cursor)
                sequence=sample['sequence'] if sample else None
                if self.sequences.get(stream,-1)!=sequence:
                    frame=await asyncio.to_thread(timeline.camera,stream,self.cursor)
                    if timeline is not self.timeline:
                        return
                    image.set_source(frame['source'] if frame else '')
                    self.sequences[stream]=sequence
                label.set_text('该时间无采样' if sample is None else f"帧 {sequence} · 采样距光标 {(self.cursor-sample['timestamp']+timeline.origin)*1000:.0f} ms")
            for stream,(label,chart) in self.tactiles.items():
                sample=await asyncio.to_thread(timeline.tactile,stream,self.cursor)
                if timeline is not self.timeline:
                    return
                label.set_text('该时间无采样' if sample is None else '合力 XYZ 原始值：'+' / '.join(f'{v:.0f}' for v in sample['resultant']))
                for index,series in enumerate(chart.options['series']):
                    series['data']=[row[index] for row in sample['taxels']] if sample else []
                chart.update()
            series=timeline.joints.get(self.joint.value,([],[]))
            for option,values in zip(self.chart.options['series'],series):
                option['data']=values[::max(1,(len(values)+699)//700)]
            self.chart.options['series'][0]['markLine']={'silent':True,'symbol':'none','label':{'show':False},'data':[{'xAxis':self.cursor}]}
            self.chart.update()
            self.joint_table.rows=timeline.joint_rows(self.cursor)
            self.joint_table.update()
        except (OSError,ValueError,KeyError,TypeError,ImportError) as exc:
            self.playing=False
            self.error.set_text(f'采样预览失败：{exc}')
        finally:
            self.rendering=False


def build_collection_view(root):
    return CollectionView(root)
