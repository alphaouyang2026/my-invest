import DataCenter from './data-center'

export default function Home() {
  return (
    <main>
      <header className="hero">
        <p className="eyebrow">PERSONAL RESEARCH TERMINAL</p>
        <h1>个人日本股票研究与模拟交易系统</h1>
        <p>以可追溯的数据版本，构建可信的历史研究。</p>
      </header>
      <DataCenter />
    </main>
  );
}
