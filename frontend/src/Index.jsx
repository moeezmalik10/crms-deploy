import { RouterProvider } from 'react-router'
import { createRoot } from 'react-dom/client'
import Routes from './Routes'

createRoot(document.getElementById('root')).render(
 <RouterProvider router={Routes}></RouterProvider>
)
