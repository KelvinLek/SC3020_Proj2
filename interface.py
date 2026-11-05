"""Member 4: Tkinter GUI. Database and AI logic are supplied by project.py.

All Tkinter updates happen on the main thread. Slow operations run in one
background worker, communicating through a queue polled with after().
"""
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText


COLOURS = {'same': '#4CAF50', 'changed': '#FFB300',
           'unmatched': '#E53935', 'helper': '#9E9E9E'}


def tree_layout(root):
    """Assign each subtree its own horizontal space; preserve child order.

    Leaves occupy consecutive columns. A parent's x coordinate is the
    midpoint of its first and last child. Runs in O(number of nodes).
    """
    nodes, edges = [], []
    next_leaf = 0

    def visit(node, depth):
        nonlocal next_leaf
        child_positions = [visit(child, depth + 1) for child in node.children]
        if child_positions:
            x = (child_positions[0][0] + child_positions[-1][0]) / 2
        else:
            x = 140 + next_leaf * 270
            next_leaf += 1
        y = 70 + depth * 115
        nodes.append((node, x, y))
        for cx, cy in child_positions:
            edges.append((x, y + 45, cx, cy - 45))
        return x, y

    visit(root, 0)
    return nodes, edges


class PlanCanvas(ttk.Frame):
    """Scrollable operator tree with selectable nodes and matching highlights."""

    def __init__(self, parent, title, on_select):
        super().__init__(parent)
        ttk.Label(self, text=title, font=('Segoe UI', 11, 'bold')).grid(
            row=0, column=0, sticky='w', padx=8, pady=5)
        self.canvas = tk.Canvas(self, background='#f8fafc', highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        vertical.grid(row=1, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        horizontal.grid(row=2, column=0, sticky='ew')
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.on_select = on_select
        self.boxes = []
        self.root_node = None
        self.scale = 1.0
        self.auto_fit = True
        self.selected_ids = ()
        self.selected_match = None
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky='e', padx=8)
        ttk.Button(toolbar, text='Fit', width=5, command=self.fit).pack(side='left')
        ttk.Button(toolbar, text='−', width=3, command=lambda: self.zoom(0.8)).pack(side='left')
        ttk.Button(toolbar, text='+', width=3, command=lambda: self.zoom(1.25)).pack(side='left')
        ttk.Button(toolbar, text='100%', width=5, command=self.reset_zoom).pack(side='left')
        self.resize_job = None
        self.canvas.bind('<Configure>', self.resized)
        self.canvas.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units'))
        self.canvas.bind('<Control-MouseWheel>', self.wheel_zoom)
        self.canvas.bind('<ButtonPress-2>', lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind('<B2-Motion>', lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))

    def resized(self, event=None):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(100, self.fit_if_needed)

    def fit_if_needed(self):
        self.resize_job = None
        if self.auto_fit:
            self.fit()

    def wheel_zoom(self, event):
        self.zoom(1.15 if event.delta > 0 else 1 / 1.15)
        return 'break'

    def fit(self):
        self.auto_fit = True
        if self.root_node is None:
            return
        nodes, _ = tree_layout(self.root_node)
        width = max(x for _, x, _ in nodes) - min(x for _, x, _ in nodes) + 240
        height = max(y for _, _, y in nodes) - min(y for _, _, y in nodes) + 90
        # Reserve fixed screen-space margins, not margins reduced by zoom.
        self.scale = max(0.01, min(1.0, max(1, self.canvas.winfo_width()-64) / width,
                                  max(1, self.canvas.winfo_height()-64) / height))
        self.render()

    def zoom(self, factor):
        if self.root_node is None:
            return
        self.auto_fit = False
        self.scale = max(0.08, min(3.0, self.scale * factor))
        self.render()

    def reset_zoom(self):
        self.auto_fit = False
        self.scale = 1.0
        self.render()

    def clear(self):
        self.root_node = None
        self.selected_ids, self.selected_match = (), None
        self.canvas.delete('all')
        self.boxes = []
        self.canvas.configure(scrollregion=(0, 0, 1, 1))

    def draw(self, root):
        self.clear()
        self.root_node = root
        self.fit()

    def render(self):
        if self.root_node is None:
            return
        self.canvas.delete('all')
        self.boxes = []
        nodes, edges = tree_layout(self.root_node)
        scale = self.scale
        for x1, y1, x2, y2 in edges:
            self.canvas.create_line(x1*scale, y1*scale, x2*scale, y2*scale, fill='#64748b', width=2)
        for i, (node, x, y) in enumerate(nodes):
            tag = f'node{i}'
            status = node.details.get('cmp_status', 'unmatched')
            colour = node.details.get('cmp_colour', COLOURS.get(status, '#9E9E9E'))
            rect = self.canvas.create_rectangle((x-120)*scale, (y-45)*scale, (x+120)*scale, (y+45)*scale,
                        fill=colour, outline='#475569', width=1, tags=tag)
            match = node.details.get('cmp_match_id')
            label = f'{node.operator}\n{node.table or ""}\n{status}'
            if match is not None:
                label += f' | match {match}'
            self.canvas.create_text(x*scale, y*scale, text=label, width=225*scale,
                                    font=('Segoe UI', -max(1, round(14*scale))), tags=tag)
            self.canvas.tag_bind(tag, '<Button-1>', lambda e, n=node: self.on_select(n))
            self.boxes.append((node, rect))
        bounds = self.canvas.bbox('all')
        if bounds:
            width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
            view_width = self.canvas.winfo_width()
            view_height = self.canvas.winfo_height()
            # Centre a fitting diagram; keep oversized diagrams reachable by scrolling.
            left = max(24, (view_width-width)/2)
            top = max(24, (view_height-height)/2)
            self.canvas.move('all', left-bounds[0], top-bounds[1])
            self.canvas.configure(scrollregion=(0, 0, max(view_width, width+48),
                                               max(view_height, height+48)))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.highlight(self.selected_ids, self.selected_match)

    def highlight(self, node_ids=(), match_id=None):
        self.selected_ids, self.selected_match = node_ids, match_id
        for node, rect in self.boxes:
            selected = (node.details.get('cmp_node_id') in node_ids or
                        (match_id is not None and node.details.get('cmp_match_id') == match_id))
            self.canvas.itemconfigure(rect, width=4 if selected else 1,
                                      outline='#0f172a' if selected else '#475569')


class QueryPlanApp(tk.Tk):
    def __init__(self, service, model):
        super().__init__()
        self.title('SC3020 | Query Plan Comparison')
        self.geometry(f'{min(1280, self.winfo_screenwidth()-60)}x{min(900, self.winfo_screenheight()-90)}')
        self.minsize(900, 650)
        self.service = service
        self.events = queue.Queue()
        self.busy = False
        self.result = None
        self.metadata = None
        self.controls = []
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._build(model)
        self.after(100, self.poll)

    def control(self, widget, state='normal'):
        self.controls.append((widget, state))
        return widget

    def _build(self, model):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Query Plan Comparison', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(outer, text=f'PostgreSQL vs Gemini  |  Configured model: {model}').pack(anchor='w', pady=(0, 8))
        self.settings_button = self.control(ttk.Button(outer, text='Hide connection settings', command=self.toggle_settings))
        self.settings_button.pack(anchor='e')
        self.settings_area = ttk.Frame(outer)
        self.settings_area.pack(fill='x')
        connection = ttk.LabelFrame(self.settings_area, text='1. Connect to PostgreSQL', padding=8)
        connection.pack(fill='x')
        self.fields = {}
        for col, (label, default, width) in enumerate([
                ('Host', 'localhost', 16), ('Port', '5432', 7),
                ('User', 'postgres', 12), ('Password', '', 18)]):
            ttk.Label(connection, text=label).grid(row=0, column=col*2, padx=(0, 5))
            entry = self.control(ttk.Entry(connection, width=width, show='*' if label == 'Password' else ''))
            entry.insert(0, default)
            entry.grid(row=0, column=col*2+1, padx=(0, 12))
            entry.bind('<KeyRelease>', self.invalidate_connection)
            self.fields[label.lower()] = entry
        self.control(ttk.Button(connection, text='Load databases', command=self.load_databases)).grid(row=0, column=8)
        selectors = ttk.Frame(self.settings_area, padding=(0, 8))
        selectors.pack(fill='x')
        ttk.Label(selectors, text='Database').pack(side='left')
        self.database = self.control(ttk.Combobox(selectors, state='readonly', width=25), 'readonly')
        self.database.pack(side='left', padx=8)
        self.database.bind('<<ComboboxSelected>>', self.load_schemas)
        ttk.Label(selectors, text='Schema').pack(side='left')
        self.schema = self.control(ttk.Combobox(selectors, state='readonly', width=22), 'readonly')
        self.schema.pack(side='left', padx=8)
        self.schema.bind('<<ComboboxSelected>>', lambda e: self.clear_result())
        ttk.Label(selectors, text='API key (optional if already configured)').pack(side='left', padx=(12, 5))
        self.api_key = self.control(ttk.Entry(selectors, show='*', width=24))
        self.api_key.pack(side='left', fill='x', expand=True)
        query_frame = ttk.LabelFrame(outer, text='2. Enter SQL', padding=8)
        query_frame.pack(fill='x')
        self.query_frame = query_frame
        self.query = self.control(ScrolledText(query_frame, height=3, wrap='word', font=('Consolas', 11)))
        self.query.pack(fill='x')
        self.query.bind('<<Modified>>', self.query_changed)
        actions = ttk.Frame(query_frame)
        actions.pack(fill='x', pady=(6, 0))
        self.control(ttk.Button(actions, text='Run comparison', command=self.run)).pack(side='left')
        self.use_cache = tk.BooleanVar(value=True)
        self.control(ttk.Checkbutton(actions, text='Reuse cached AI answer when available',
                                    variable=self.use_cache)).pack(side='left', padx=12)
        self.export = ttk.Button(actions, text='Export JSON', command=self.export_result, state='disabled')
        self.export.pack(side='right')
        self.status = tk.StringVar(value='Enter your PostgreSQL password, then click Load databases.')
        ttk.Label(outer, textvariable=self.status, wraplength=1150).pack(anchor='w', pady=6)
        self.progress = ttk.Progressbar(outer, mode='indeterminate')
        self.progress.pack(fill='x')
        self.progress.pack_forget()
        legend = ttk.Frame(outer)
        legend.pack(fill='x', pady=6)
        for status, colour in COLOURS.items():
            tk.Label(legend, text=f'  {status}  ', bg=colour).pack(side='left', padx=(0, 8))
        ttk.Label(legend, text='Click nodes for details. Fit shows the full tree; +/− zoom. Drag the divider to resize results.').pack(side='left')
        self.summary = tk.StringVar(value='No comparison yet.')
        ttk.Label(outer, textvariable=self.summary, wraplength=1150).pack(anchor='w', pady=(0, 6))
        panes = ttk.Panedwindow(outer, orient='vertical')
        self.panes = panes
        panes.pack(fill='both', expand=True)
        trees = ttk.Panedwindow(panes, orient='horizontal')
        self.pg = PlanCanvas(trees, 'PostgreSQL plan', self.select_node)
        self.ai = PlanCanvas(trees, 'Gemini plan', self.select_node)
        trees.add(self.pg, weight=1)
        trees.add(self.ai, weight=1)
        panes.add(trees, weight=4)
        tabs = ttk.Notebook(panes)
        panes.add(tabs, weight=2)
        differences = ttk.Frame(tabs)
        tabs.add(differences, text='Differences')
        self.diff_list = tk.Listbox(differences, width=44, height=7, exportselection=False)
        self.diff_list.pack(side='left', fill='y')
        self.diff_list.bind('<<ListboxSelect>>', self.select_difference)
        self.diff_text = self.readonly_text(differences)
        self.same_text = self.readonly_text(tabs)
        self.node_text = self.readonly_text(tabs)
        self.report_text = self.readonly_text(tabs)
        tabs.add(self.same_text, text='Similarities')
        tabs.add(self.node_text, text='Selected node')
        tabs.add(self.report_text, text='Full report')
        self.tabs = tabs
        self.after(200, self.position_divider)

    def position_divider(self):
        height = self.panes.winfo_height()
        if height > 100:
            self.panes.sashpos(0, int(height * 0.67))

    def toggle_settings(self):
        if self.settings_area.winfo_manager():
            self.settings_area.pack_forget()
            self.settings_button.configure(text='Show connection settings')
        else:
            self.settings_area.pack(fill='x', before=self.query_frame)
            self.settings_button.configure(text='Hide connection settings')
        self.after(100, self.position_divider)

    @staticmethod
    def readonly_text(parent):
        widget = ScrolledText(parent, wrap='word', height=8, font=('Segoe UI', 10), state='disabled')
        widget.pack(fill='both', expand=True)
        return widget

    @staticmethod
    def put(widget, text):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')
        widget.yview_moveto(0)

    def clear_result(self):
        self.result = self.metadata = None
        self.pg.clear()
        self.ai.clear()
        self.diff_list.delete(0, 'end')
        for widget in (self.diff_text, self.same_text, self.node_text, self.report_text):
            self.put(widget, '')
        self.summary.set('No comparison for the current inputs. Click Run comparison.')
        self.export.configure(state='disabled')

    def query_changed(self, event=None):
        if self.query.edit_modified():
            self.clear_result()
            self.query.edit_modified(False)

    def invalidate_connection(self, event=None):
        self.database.set('')
        self.database['values'] = ()
        self.schema.set('')
        self.schema['values'] = ()
        self.clear_result()

    def settings(self):
        values = {key: entry.get() for key, entry in self.fields.items()}
        try:
            values['port'] = int(values['port'])
            if not 1 <= values['port'] <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError('Port must be a number between 1 and 65535.') from None
        if not values['host'].strip() or not values['user'].strip():
            raise ValueError('Enter both a host and a user.')
        return values

    def start(self, work, done, status):
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        self.progress.pack(fill='x', before=self.panes)
        self.progress.start(12)
        for widget, state in self.controls:
            widget.configure(state='disabled')
        self.export.configure(state='disabled')

        def worker():
            try:
                self.events.put(('done', (done, work())))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.status.set(value)
                    continue
                self.busy = False
                self.progress.stop()
                self.progress.pack_forget()
                for widget, state in self.controls:
                    widget.configure(state=state)
                if kind == 'error':
                    self.status.set('Operation failed. Check the error, then try again.')
                    messagebox.showerror('Could not complete operation', value, parent=self)
                else:
                    callback, payload = value
                    try:
                        callback(payload)
                    except Exception as exc:
                        self.clear_result()
                        self.status.set('Could not display the result.')
                        messagebox.showerror('Display error', str(exc), parent=self)
                self.export.configure(state='normal' if self.result else 'disabled')
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def load_databases(self):
        try:
            settings = self.settings()
        except ValueError as exc:
            messagebox.showerror('Connection settings', str(exc), parent=self)
            return
        self.invalidate_connection()
        self.start(lambda: self.service.databases(settings), self.databases_loaded, 'Connecting to PostgreSQL...')

    def databases_loaded(self, names):
        self.database['values'] = names
        self.status.set('Choose a database.' if names else 'No user databases found on this server.')
        if names:
            self.database.set(names[0])
            self.load_schemas()

    def load_schemas(self, event=None):
        self.clear_result()
        self.schema.set('')
        self.schema['values'] = ()
        settings, database = self.settings(), self.database.get()
        self.start(lambda: self.service.schemas(settings, database), self.schemas_loaded, 'Loading schemas...')

    def schemas_loaded(self, names):
        self.schema['values'] = names
        if names:
            self.schema.set(names[0])
        self.status.set('Enter SQL and click Run comparison.' if names else 'This database has no table-containing schemas.')

    def run(self):
        try:
            settings = self.settings()
            database, schema = self.database.get(), self.schema.get()
            query = self.query.get('1.0', 'end').strip()
            if not database or not schema:
                raise ValueError('Load databases and select a database and schema first.')
            if not query:
                raise ValueError('Enter an SQL query first.')
        except ValueError as exc:
            messagebox.showerror('Check your inputs', str(exc), parent=self)
            return
        api_key, use_cache = self.api_key.get().strip(), self.use_cache.get()
        self.clear_result()
        self.start(lambda: self.service.compare(settings, database, schema, query, api_key,
                    use_cache, lambda text: self.events.put(('progress', text))),
                   self.show_result, 'Starting comparison...')

    def show_result(self, payload):
        if self.settings_area.winfo_manager():
            self.toggle_settings()
        self.result, self.metadata = payload
        result = self.result
        self.pg.draw(result.pg_tree)
        self.ai.draw(result.ai_tree)
        self.after(150, self.position_divider)
        self.summary.set(f'Difference rate: {result.difference_rate:.2%}  |  '
                         f'Including helpers: {result.strict_difference_rate:.2%}  |  '
                         f'Model: {self.metadata["model"]}\n'
                         'Rates measure structural differences, not AI accuracy or query speed.')
        for number, difference in enumerate(result.differences, 1):
            self.diff_list.insert('end', f'{number}. {difference.title} [{difference.severity}]')
        self.put(self.diff_text, 'Select a difference on the left.' if result.differences else 'No differences reported.')
        self.put(self.same_text, '\n\n'.join(result.similarities))
        self.put(self.report_text, result.report())
        self.status.set('Comparison complete. Click nodes or differences to explore the result.')

    def select_node(self, node):
        match = node.details.get('cmp_match_id')
        ids = [node.details.get('cmp_node_id')]
        self.pg.highlight(ids, match)
        self.ai.highlight(ids, match)
        data = node.to_dict()
        data.pop('children', None)
        self.put(self.node_text, json.dumps(data, indent=2, ensure_ascii=False, default=str))
        self.tabs.select(self.node_text)

    def select_difference(self, event=None):
        selection = self.diff_list.curselection()
        if self.result is None or not selection:
            return
        difference = self.result.differences[selection[0]]
        self.put(self.diff_text, f'{difference.title} ({difference.severity})\n\n{difference.explanation}')
        ids = [node.details.get('cmp_node_id') for node in difference.pg_nodes + difference.ai_nodes]
        self.pg.highlight(ids)
        self.ai.highlight(ids)

    def export_result(self):
        if not self.result:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.json',
                    initialfile='comparison_result.json', filetypes=[('JSON', '*.json')])
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as handle:
                    json.dump({'run': self.metadata,
                               'comparison': self.result.to_dict(include_trees=True)},
                              handle, indent=2, ensure_ascii=False, default=str)
                self.status.set('Comparison exported successfully.')
            except OSError as exc:
                messagebox.showerror('Export failed', str(exc), parent=self)

    def close(self):
        if self.busy and not messagebox.askyesno('Close application?',
                'A request is running. Closing will discard its result; an API request already sent may still count toward usage. Close?', parent=self):
            return
        self.destroy()
