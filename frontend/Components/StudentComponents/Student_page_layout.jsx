import { Outlet } from "react-router"
import StudentSideBar from "./StudentSideBar"
export default function StudentPageLayout(){
return(
<>
    <StudentSideBar />
    <div className="lg:flex lg:ml-72  rounded-2xl bg-gray-100">
    <Outlet  />

    </div>
</>

)

}