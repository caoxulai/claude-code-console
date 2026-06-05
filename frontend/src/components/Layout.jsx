import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import NavBar from './NavBar';
import TopBar from './TopBar';

export default function Layout() {
  const [navOpen, setNavOpen] = useState(window.innerWidth > 768);

  return (
    <div className={`shell ${navOpen ? '' : 'nav-collapsed'}`}>
      <TopBar onToggleNav={() => setNavOpen(!navOpen)} />
      {navOpen && <NavBar onClose={() => setNavOpen(false)} />}
      <main className="main-content">
        <Outlet />
      </main>
    </div>
  );
}
