export type LifecycleFilter = 'all' | 'active' | 'archived';
export type CompletionFilter = 'all' | 'empty' | 'unfinished' | 'completed';
export type UpdatedSort = 'updated-desc' | 'updated-asc';

function FilterButtonGroup<TValue extends string>(props: {
  label: string;
  value: TValue;
  options: Array<{ value: TValue; label: string }>;
  onChange(value: TValue): void;
}) {
  return (
    <div className="fj-review-filter-group" role="radiogroup" aria-label={props.label}>
      <span>{props.label}</span>
      <div>
        {props.options.map((option) => (
          <button
            key={option.value}
            className={props.value === option.value ? 'is-active' : undefined}
            type="button"
            role="radio"
            aria-checked={props.value === option.value}
            onClick={() => props.onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function ProjectListControls(props: {
  query: string;
  lifecycleFilter: LifecycleFilter;
  completionFilter: CompletionFilter;
  updatedSort: UpdatedSort;
  pageSize: number;
  onQueryChange(value: string): void;
  onLifecycleChange(value: LifecycleFilter): void;
  onCompletionChange(value: CompletionFilter): void;
  onUpdatedSortChange(value: UpdatedSort): void;
  onPageSizeChange(value: number): void;
}) {
  return (
    <section className="fj-review-list-toolbar" aria-label="项目列表筛选">
      <label>
        <span>搜索项目</span>
        <input
          aria-label="搜索项目"
          placeholder="项目名称或编号"
          type="search"
          value={props.query}
          onChange={(event) => props.onQueryChange(event.target.value)}
        />
      </label>
      <FilterButtonGroup<LifecycleFilter>
        label="生命周期筛选"
        value={props.lifecycleFilter}
        options={[
          { value: 'all', label: '全部' },
          { value: 'active', label: '进行中' },
          { value: 'archived', label: '已归档' },
        ]}
        onChange={props.onLifecycleChange}
      />
      <FilterButtonGroup<CompletionFilter>
        label="完成状态筛选"
        value={props.completionFilter}
        options={[
          { value: 'all', label: '全部' },
          { value: 'empty', label: '未创建成片' },
          { value: 'unfinished', label: '未完成' },
          { value: 'completed', label: '已完成' },
        ]}
        onChange={props.onCompletionChange}
      />
      <FilterButtonGroup<UpdatedSort>
        label="更新时间排序"
        value={props.updatedSort}
        options={[
          { value: 'updated-desc', label: '新到旧' },
          { value: 'updated-asc', label: '旧到新' },
        ]}
        onChange={props.onUpdatedSortChange}
      />
      <FilterButtonGroup<'20' | '50' | '100'>
        label="每页数量"
        value={String(props.pageSize) as '20' | '50' | '100'}
        options={[
          { value: '20', label: '20' },
          { value: '50', label: '50' },
          { value: '100', label: '100' },
        ]}
        onChange={(value) => props.onPageSizeChange(Number(value))}
      />
    </section>
  );
}
