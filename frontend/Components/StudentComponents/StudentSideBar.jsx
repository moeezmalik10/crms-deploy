import { LogOut } from "lucide-react";
import { NavLink } from "react-router";
import { useNavigate } from "react-router";
import { Menu, X } from "lucide-react";
import React, { useState } from "react";
export default function StudentSideBar() {
  const [isOpen, setIsOpen] = useState(true);
  const username = localStorage.getItem("username");
  const navigate = useNavigate();


  const handleLogOut = () => {
  localStorage.removeItem("token")
  localStorage.removeItem("user_id")
  localStorage.removeItem("username")
  localStorage.removeItem("role")
   localStorage.removeItem("email")

  navigate("/")
};

  return (
    <div>
     {isOpen && (
          <div
            className="lg:hidden inset-0 fixed bg-black/40 z-40"
            onClick={() => setIsOpen(false)}
          />
        )}

{/* ============================= Menu Button ================== */}
      <nav className=" ">
        <NavLink className="lg:hidden">
          <Menu onClick={() => setIsOpen(true)} />
        </NavLink>
       {/* ====================================SIDEBAR======== */}
        <div
          className={` h-full  bg-[#1e1e1e] px-6 py-4 fixed top-0 left-0 z-200 transform transition-transform duration-300 ease-in-out flex flex-col justify-between  w-72
          ${isOpen ? "translate-x-0" : "-translate-x-full"} lg:translate-x-0`}
        >
          {/* -===========================X button */}
          <div >
            <div className="flex justify-between p-2  mt-2 mb-10 bg-white rounded-lg  ">
                <NavLink to="studentprofile">
            <h2 className="font-bold text-2xl">{username || "Student"}</h2>
          </NavLink>
            <button   className="lg:hidden" onClick={() => setIsOpen(false)}>
              <X className="" size={28} />
            </button>
            </div>

            {/* =======================New Request============ */}
            <div className=" ">
              <div className="flex flex-col gap-5 w-full  ">
                <NavLink
                  to={"newrequest"}
                  onClick={() => setIsOpen(false)}
                  className={({
                    isActive,
                  }) => `  p-2 rounded-lg text-start w-full transition-all duration-300 tracking-wider 
                ${isActive ? "bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]  "}
                `}
                >
                  {" "}
                  New Request
                </NavLink>



                  {/* ================================ML task reques======== */}
                <NavLink
                 onClick={() => setIsOpen(false)}
                  className={({ isActive }) => ` p-3 rounded-lg tracking-wider text-start w-full transition-all duration-300
                ${isActive ? " bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]"}
                `}
                to={"mltaskrequest"}
                >
                  ML task Request
                </NavLink>
                  {/* ================================ML task result======== */}
                <NavLink
                 onClick={() => setIsOpen(false)}
                  className={({ isActive }) => ` p-3 rounded-lg tracking-wider text-start w-full transition-all duration-300
                ${isActive ? " bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]"}
                `}
                to={"mltaskresults"}
                >
                  ML task Results
                </NavLink>


                {/* ================================Remote Link======== */}
                <NavLink
                 onClick={() => setIsOpen(false)}
                  to={"studentsessionlink"}
                  className={({
                    isActive,
                  }) => `  p-2 rounded-lg text-start w-full transition-all duration-300 tracking-wider
                ${isActive ? " bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]"}
                `}
                >
                  Remote Link
                </NavLink>
                {/* ================================Share this device======== */}
                <NavLink
                 onClick={() => setIsOpen(false)}
                  className={({ isActive }) => ` p-3 rounded-lg tracking-wider text-start w-full transition-all duration-300
                ${isActive ? " bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]"}
                `}
                to={"sharedevice"}
                >
                  Share this device
                </NavLink>
                {/* ================================Sessions======== */}
                <NavLink
                 onClick={() => setIsOpen(false)}
                  className={({ isActive }) => ` p-3 rounded-lg tracking-wider text-start w-full transition-all duration-300
                ${isActive ? " bg-[#b3b3b3ee] text-black font-bold" : "text-white bg-[#212121]"}
                `}
                to={"studentsessions"}
                >
                  Sessions
                </NavLink>
              
              </div>
            </div>
          </div>

          <div>
            <button onClick={handleLogOut} className=" flex  bg-[#f44336] p-6 rounded-xl w-full justify-around h-4 items-center">
              Logout
              <LogOut className="text-white" />
            </button>
          </div>
        </div>
      </nav>
    </div>
  );
}

  {/* =============================top navbar====== */}
  {/* <nav className="">
    <div className="flex justify-between w-full p-2 font-bold text-xl shadow-xl rounded-xl bg-[#eee] mb-8">
      <NavLink to="adminprofile">
        <h2>{username || "Admin"}</h2>
      </NavLink>
      <button>
        <LogOut onClick={handleLogOut} />
      </button>
    </div>
    <hr />
    <div className=" font-bold flex justify-around rounded-xl p-2">
      <NavLink
        to={"history"}
        className={({
          isActive,
        }) => `  p-2 rounded-2xl w-28 transition-all duration-300
    ${isActive ? "bg-[#b3b3b3ee] text-black" : "bg-white text-black"}
    `}
      >
        {" "}
        History
      </NavLink>
      <NavLink
        to={"machines"}
        className={({
          isActive,
        }) => ` p-2 rounded-2xl w-28 transition-all duration-300   
    ${isActive ? " bg-[#b3b3b3ee] text-black" : "bg-white text-black"}
    `}
      >
        Machines
      </NavLink>
      <NavLink
        className={({ isActive }) => ` w-fit
    p-2 rounded-2xl  transition-all duration-300
    ${isActive ? " bg-[#b3b3b3ee] text-black" : "bg-white text-black"}
    `}
        to={"manageusers"}
      >
        Manage Users
      </NavLink>
    </div>
    <hr />

  </nav> */}