import { createBrowserRouter } from "react-router"
import App from "./App"
import LoginPage from "../Components/LoginPage/LoginPage"
import AdminPageLayout from "../Components/Admin_Page_Components/AdminPageLayout"
import AdminProfile from "../Components/Admin_Page_Components/AdminProfile"
import History from "../Components/Admin_Page_Components/History"
import ManageUsers from "../Components/Admin_Page_Components/ManageUsers"
import Machine from "../Components/Admin_Page_Components/Machines"


import StudentPageLayout from "../Components/StudentComponents/Student_page_layout"
import StudentProfile from "../Components/StudentComponents/StudentProfile"
import StudentSessionLink from "../Components/StudentComponents/StudentSessionLink"
import StudentSessions from "../Components/StudentComponents/StudentSessions"
import NewRequests from "../Components/StudentComponents/NewRequest"
import MLTask from "../Components/StudentComponents/MLTask"
import MLResults from "../Components/StudentComponents/MLResults"
import ShareDevice from "../Components/StudentComponents/ShareDevice"
import PoolDashboard from "../Components/Pool/PoolDashboard"
import Contribute from "../Components/Pool/Contribute"
import Jobs from "../Components/Pool/Jobs"
import Storage from "../Components/Pool/Storage"

import AdminSecurityDashboard from "../Components/IDS/AdminSecurityDashboard"
import BlacklistDashboard from "../Components/IDS/BlacklistDashboard"

import { Component } from "react"

const Routes = createBrowserRouter([
    {
    path:"/",
    Component:LoginPage,
},
  // Any phone / tablet can join the pool from here without logging in
  { path:"/node", Component: ShareDevice },
  { path:"/studentpage",
   Component: StudentPageLayout,
 children:[
     {index: true , Component: NewRequests, },
        {path:"studentprofile" , Component: StudentProfile, },
        {path:"newrequest" , Component: NewRequests, },
        {path:"studentsessionlink" , Component: StudentSessionLink, },
        {path:"studentsessions" , Component: StudentSessions, },
        {path:"mltaskrequest" , Component: MLTask, },
   { path:"mltaskresults", Component: MLResults },
        {path:"sharedevice" , Component: ShareDevice, },
        {path:"pool" , Component: PoolDashboard, },
        {path:"contribute" , Component: Contribute, },
        {path:"jobs" , Component: Jobs, },
        {path:"storage" , Component: Storage, },
                { path:"mltaskresults/:jobId" , Component: MLResults }

    ]
},


  { path:"/adminpage",
   Component: AdminPageLayout,
    children:[
             {index: true , Component: Machine, },
             {path:"machines" , Component: Machine, },
             {path:"pool" , Component: () => <PoolDashboard admin />, },
             {path:"history" , Component: History, },
        {path:"manageusers" , Component: ManageUsers, },
        {path:"adminprofile" , Component: AdminProfile, },
        {path:"securitydashboard", Component: AdminSecurityDashboard },
        {path:"blacklistdashboard", Component: BlacklistDashboard},
        
        
    ]
},
])





export default  Routes