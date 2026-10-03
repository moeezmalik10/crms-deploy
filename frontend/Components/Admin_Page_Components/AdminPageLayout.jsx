import React from "react"
import { Outlet } from "react-router"
import { useNavigate } from "react-router"
import AdminSideBar from "./AdminSidebar"
export default function AdminPageLayout(){
return(
    <>
    <AdminSideBar />
    <div className="lg:ml-60 rounded-2xl bg-gray-100">

    <Outlet />
    </div>
    </>
)

}